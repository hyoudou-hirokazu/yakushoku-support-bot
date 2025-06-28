import os
import logging
from flask import Flask, request, abort
import datetime
import time # 時間計測のために追加

# LINE Bot SDK v3 のインポート
from linebot.v3.webhook import WebhookHandler
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest
# !!! 修正: GetProfileRequest のインポートパスを変更しました !!!
from linebot.v3.messaging.models import GetProfileRequest # GetProfileRequest は models サブモジュールにあります
from linebot.v3.messaging import TextMessage as LineReplyTextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.exceptions import InvalidSignatureError, LineBotApiError

# Google Generative AI SDK のインポート
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# ロギング設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app = Flask(__name__)

# .envファイルから環境変数を読み込む（Renderでは環境変数が自動的に設定されるため、この行はコメントアウトを維持）
# from dotenv import load_dotenv
# load_dotenv()

# 環境変数からLINEとGeminiのAPIキーを取得
CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# 環境変数が設定されているか確認
if not CHANNEL_ACCESS_TOKEN:
    logging.critical("CHANNEL_ACCESS_TOKEN is not set in environment variables.")
    raise ValueError("CHANNEL_ACCESS_TOKEN is not set. Please set it in Render Environment Variables.")
if not CHANNEL_SECRET:
    logging.critical("CHANNEL_SECRET is not set in environment variables.")
    raise ValueError("CHANNEL_SECRET is not set. Please set it in Render Environment Variables.")
if not GEMINI_API_KEY:
    logging.critical("GEMINI_API_KEY is not set in environment variables.")
    raise ValueError("GEMINI_API_KEY is not set. Please set it in Render Environment Variables.")
# PORT環境変数がない場合のエラーチェック。Gunicornがこれを必要とするため。
if not os.getenv('PORT'):
    logging.critical("PORT environment variable is not set by Render. This is unexpected for a Web Service.")
    raise ValueError("PORT environment variable is not set. Ensure this is deployed on a platform like Render.")

# LINE Messaging API v3 の設定
try:
    configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
    line_bot_api = MessagingApi(ApiClient(configuration))
    handler = WebhookHandler(CHANNEL_SECRET)
    logging.info("LINE Bot SDK configured successfully.")
except Exception as e:
    logging.critical(f"Failed to configure LINE Bot SDK: {e}. Please check CHANNEL_ACCESS_TOKEN and CHANNEL_SECRET.")
    raise Exception(f"LINE Bot SDK configuration failed: {e}")

# Gemini API の設定
try:
    genai.configure(api_key=GEMINI_API_KEY)
    # ユーザー指定のモデル名 'gemini-2.5-flash-lite-preview-06-17' を使用
    gemini_model = genai.GenerativeModel(
        'gemini-2.5-flash-lite-preview-06-17',
        safety_settings={
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
    )
    logging.info("Gemini API configured successfully using 'gemini-2.5-flash-lite-preview-06-17' model.")
except Exception as e:
    logging.critical(f"Failed to configure Gemini API: {e}. Please check GEMINI_API_KEY and 'google-generativeai' library version in requirements.txt. Also ensure 'gemini-2.5-flash-lite-preview-06-17' model is available for your API Key/Region.")
    raise Exception(f"Gemini API configuration failed: {e}")

# --- チャットボット関連の設定 ---
MAX_GEMINI_REQUESTS_PER_DAY = 20    # 1ユーザーあたり1日20回まで (無料枠考慮)

# プロンプトを社会福祉法人SHIPの支援者向けサポートAIに調整
# ボット名を「支援メイトBot」に変更し、提供された構造と条件を反映
# --- 最適化のポイント: プロンプトの簡潔化 ---
# このプロンプトは非常に長いです。LLMの応答速度は入力トークン数に大きく依存します。
# 応答速度を向上させるためには、本当に必要な指示や情報に絞り込み、冗長な表現を削除することを強く推奨します。
# 例として、コメントでさらに簡潔化の方向性を示しますが、具体的な調整は機能性と速度のバランスを考慮して行ってください。
MANAGEMENT_SUPPORT_SYSTEM_PROMPT = """
あなたは障害福祉施設の管理職向けAIサポート「役職者お悩みサポート」です。
組織運営、人材育成、利用者支援、事業展開、法令遵守に関する悩みに、傾聴と共感を持ち、実践的かつ具体的なアドバイスを端的に提供してください。

**重要事項:**
1.  **傾聴と共感:** ユーザーの感情を汲み取りねぎらう。
2.  **事業所種別配慮:** （例: グループホーム、B型など）特性を踏まえたアドバイス。
3.  **コーチング:** ユーザーの思考を深掘り、強みと行動を促すオープンな質問。
4.  **実践的アドバイス:** 簡潔に2-3点の行動指針。
5.  **肯定的な問いかけ:** 回答の最後に建設的な質問を含める。
6.  **AIの限界:** 必要に応じ専門家への相談を促す。
7.  **簡潔さ:** 無駄なトークン消費を避ける。
トーン: 丁寧、落ち着き、寄り添い。専門用語は避け、分かりやすく。
"""
# これは例です。内容をさらに絞り込めるか検討してください。

# 初期メッセージ
INITIAL_MESSAGE = """
「役職者お悩みサポート」へようこそ。
日々の事業所運営、職員の育成、利用者様への支援、多岐にわたる管理職のお仕事、本当にお疲れ様です。
利用者様の笑顔、職員の成長、そして地域への貢献のために日々尽力されていること、心より敬意を表します。

組織運営、人材育成、利用者様対応、トラブル対応、はたまたご自身のキャリアの悩みまで、お一人で抱え込まず、どんな些細なことでも構いませんので、今お悩みのことを気軽にご相談ください。
私が、あなたの「相談役」として、最適な方向性を見つけるお手伝いをいたします。
"""

# Gemini API利用制限時のメッセージ
GEMINI_LIMIT_MESSAGE = """
申し訳ありません、本日の「役職者お悩みサポート」のご利用回数の上限に達しました。
日々の激務の中、ご活用いただきありがとうございます。
明日またお話しできますので、その時まで少しお仕事から離れて、ご自身の心身を労わってくださいね。

もし緊急を要するご質問や、詳細な情報が必要な場合は、法人本部や関係部署、関連機関にご相談ください。
明日、またお会いできることを楽しみにしております。
"""

MAX_CONTEXT_TURNS = 6 # (ユーザーの発言 + AIの返答) の合計ターン数、トークン消費と相談して調整

# !!! 重要: この user_sessions はメモリ上にあるため、Renderのスピンダウン/再起動でリセットされます。
# !!! 永続化にはPostgreSQL, Redis, Firestoreなどを利用することを強く推奨します。
# !!! 例として、簡略化した形でDBを使わず記述していますが、本番運用では必ず永続化してください。
user_sessions = {}

@app.route("/callback", methods=['POST'])
def callback():
    start_callback_time = time.time() # コールバック処理全体の開始時刻
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)

    if not signature:
        app.logger.error("X-Line-Signature header is missing.")
        abort(400)

    app.logger.info(f"[{time.time() - start_callback_time:.3f}s] Received Webhook Request.")
    app.logger.info("  Request body (truncated to 500 chars): " + body[:500])
    app.logger.info(f"  X-Line-Signature: {signature}")

    # --- LINE Bot SDKによる署名検証とイベント処理 ---
    try:
        handler.handle(body, signature)
        app.logger.info(f"[{time.time() - start_callback_time:.3f}s] Webhook handled successfully by SDK.")
    except InvalidSignatureError:
        app.logger.error(f"[{time.time() - start_callback_time:.3f}s] !!! SDK detected Invalid signature !!!")
        app.logger.error("  This typically means CHANNEL_SECRET in Render does not match LINE Developers.")
        app.logger.error(f"  Body (truncated for error log): {body[:200]}...")
        app.logger.error(f"  Signature sent to SDK: {signature}")
        app.logger.error(f"  Channel Secret configured for SDK (first 5 chars): {CHANNEL_SECRET[:5]}...")
        abort(400) # 署名エラーの場合は400を返す
    except Exception as e:
        # その他の予期せぬエラー
        logging.critical(f"[{time.time() - start_callback_time:.3f}s] Unhandled error during webhook processing by SDK: {e}", exc_info=True)
        abort(500)

    app.logger.info(f"[{time.time() - start_callback_time:.3f}s] Total callback processing time.")
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    start_handle_time = time.time() # handle_message 処理開始時刻を記録
    user_id = event.source.user_id
    user_message = event.message.text
    app.logger.info(f"[{time.time() - start_handle_time:.3f}s] handle_message started for user_id: '{user_id}', message: '{user_message}' (Reply Token: {event.reply_token})")

    response_text = "申し訳ありません、現在メッセージを処理できません。しばらくしてからもう一度お試しください。"

    current_date = datetime.date.today()

    # --- ユーザーセッションの初期化または取得 ---
    if user_id not in user_sessions or user_sessions[user_id]['last_request_date'] != current_date:
        app.logger.info(f"[{time.time() - start_handle_time:.3f}s] Initializing/Resetting session for user_id: {user_id}. First message of the day or new user.")
        user_sessions[user_id] = {
            'history': [],
            'request_count': 0,
            'last_request_date': current_date,
            'display_name': "管理者" # デフォルト値を設定
        }

        # ユーザー名取得 (初回のみ)
        # 永続化されたセッションにdisplay_nameを保存すれば、次回以降はAPI呼び出し不要
        # 現状ではアプリ再起動でリセットされるため、毎回初回はAPIを叩く
        start_get_profile = time.time()
        try:
            profile_response = line_bot_api.get_profile(GetProfileRequest(user_id=user_id))
            if profile_response and hasattr(profile_response, 'display_name'):
                user_sessions[user_id]['display_name'] = profile_response.display_name
                app.logger.info(f"[{time.time() - start_get_profile:.3f}s] Fetched display name for user {user_id}: {user_sessions[user_id]['display_name']}")
            else:
                app.logger.warning(f"[{time.time() - start_get_profile:.3f}s] Could not get display name for user {user_id}.")
        except LineBotApiError as e:
            app.logger.error(f"[{time.time() - start_get_profile:.3f}s] LineBotApiError getting user profile for {user_id}: {e}", exc_info=True)
        except Exception as e:
            app.logger.error(f"[{time.time() - start_get_profile:.3f}s] Unexpected error getting user profile for {user_id}: {e}", exc_info=True)

        user_display_name = user_sessions[user_id]['display_name']

        # パーソナライズされた初期メッセージを生成
        personalized_initial_message = (
            f"{user_display_name}さん、" + INITIAL_MESSAGE
        )
        response_text = personalized_initial_message

        try:
            start_reply_initial = time.time()
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[LineReplyTextMessage(text=response_text)]
                )
            )
            app.logger.info(f"[{time.time() - start_reply_initial:.3f}s] Sent personalized initial message/daily reset message to user {user_id}.")
        except Exception as e:
            logging.error(f"Error sending personalized initial/reset reply to LINE for user {user_id}: {e}", exc_info=True)
        app.logger.info(f"[{time.time() - start_handle_time:.3f}s] handle_message finished for initial/reset flow.")
        return 'OK' # 初回メッセージ送信後はここで処理を終了。この返信はGeminiを呼び出さない。

    # Gemini API利用回数制限のチェック
    if user_sessions[user_id]['request_count'] >= MAX_GEMINI_REQUESTS_PER_DAY:
        response_text = GEMINI_LIMIT_MESSAGE
        app.logger.warning(f"User {user_id} exceeded daily Gemini request limit ({MAX_GEMINI_REQUESTS_PER_DAY}).")
        try:
            start_reply_limit = time.time()
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[LineReplyTextMessage(text=response_text)]
                )
            )
            app.logger.info(f"[{time.time() - start_reply_limit:.3f}s] Sent limit message to LINE for user {user_id}.")
        except Exception as e:
            logging.error(f"Error sending limit reply to LINE for user {user_id}: {e}", exc_info=True)
        app.logger.info(f"[{time.time() - start_handle_time:.3f}s] handle_message finished for limit exceeded flow.")
        return 'OK'

    # 会話履歴を準備
    # システムプロンプトと初期応答を履歴の最初に含める
    chat_history_for_gemini = [
        {'role': 'user', 'parts': [{'text': MANAGEMENT_SUPPORT_SYSTEM_PROMPT}]},
        {'role': 'model', 'parts': [{'text': "はい、承知いたしました。管理職の皆様のお力になれるよう、「役職者お悩みサポート」が心を込めてお話を伺います。"}]}
    ]

    start_index = max(0, len(user_sessions[user_id]['history']) - MAX_CONTEXT_TURNS * 2)
    app.logger.debug(f"[{time.time() - start_handle_time:.3f}s] Current history length for user {user_id}: {len(user_sessions[user_id]['history'])}. Taking from index {start_index}.")

    for role, text_content in user_sessions[user_id]['history'][start_index:]:
        chat_history_for_gemini.append({'role': role, 'parts': [{'text': text_content}]})

    app.logger.debug(f"[{time.time() - start_handle_time:.3f}s] Gemini chat history prepared for user {user_id} (last message: '{user_message}'): {chat_history_for_gemini}")

    try:
        start_gemini_call = time.time() # Gemini呼び出し前を計測
        convo = gemini_model.start_chat(history=chat_history_for_gemini)
        gemini_response = convo.send_message(user_message)
        end_gemini_call = time.time() # Gemini呼び出し後を計測
        app.logger.info(f"[{end_gemini_call - start_gemini_call:.3f}s] Gemini API call completed for user {user_id}.")

        if gemini_response and hasattr(gemini_response, 'text'):
            response_text = gemini_response.text
        elif isinstance(gemini_response, list) and gemini_response and hasattr(gemini_response[0], 'text'):
            response_text = gemini_response[0].text
        else:
            logging.warning(f"[{time.time() - start_handle_time:.3f}s] Unexpected Gemini response format or no text content: {gemini_response}")
            response_text = "Geminiからの応答形式が予期せぬものでした。"

        app.logger.info(f"[{time.time() - start_handle_time:.3f}s] Gemini generated response for user {user_id}: '{response_text}'")

        # 会話履歴を更新 (user_sessionsに保存)
        user_sessions[user_id]['history'].append(['user', user_message])
        user_sessions[user_id]['history'].append(['model', response_text])
        user_sessions[user_id]['request_count'] += 1
        user_sessions[user_id]['last_request_date'] = current_date
        app.logger.info(f"[{time.time() - start_handle_time:.3f}s] User {user_id} - Request count: {user_sessions[user_id]['request_count']}")

    except Exception as e:
        logging.error(f"[{time.time() - start_handle_time:.3f}s] Error interacting with Gemini API for user {user_id}: {e}", exc_info=True)
        response_text = "Geminiとの通信中にエラーが発生しました。時間を置いてお試しください。"

    finally:
        # LINEへの返信処理の前後を計測
        start_reply_line = time.time()
        try:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[LineReplyTextMessage(text=response_text)]
                )
            )
            app.logger.info(f"[{time.time() - start_reply_line:.3f}s] Reply sent to LINE successfully for user {user_id}.")
        except Exception as e:
            logging.error(f"Error replying to LINE for user {user_id}: {e}", exc_info=True)

    app.logger.info(f"[{time.time() - start_handle_time:.3f}s] Total handle_message processing time.")
    return 'OK'

if __name__ == "__main__":
    # Render環境ではPORT環境変数が設定されるため、それを使用する
    # ローカル実行時にはデフォルトで8080を使用
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
