import os
import logging
from flask import Flask, request, abort
# from dotenv import load_dotenv # Renderでは環境変数が自動的に設定されるため、この行はコメントアウト
import datetime
# import time # 応答性向上のため、強制的な遅延処理は削除。必要であれば再導入検討。
# import random # 遅延処理を削除したため不要。必要であれば再導入検討。

# LINE Bot SDK v3 のインポート
from linebot.v3.webhook import WebhookHandler
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest
from linebot.v3.messaging import TextMessage as LineReplyTextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
# LineBotApiErrorのインポートパスをlinebot.exceptionsに変更
from linebot.exceptions import InvalidSignatureError, LineBotApiError # LineBotApiErrorのパスを修正

# 署名検証のためのライブラリをインポート (LINE Bot SDKが内部で処理するため通常は不要だが、デバッグ用として残す)
import hmac
import hashlib
import base64

# Google Generative AI SDK のインポート
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# ロギング設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app = Flask(__name__)

# .envファイルから環境変数を読み込む（Renderでは不要だが、ローカル実行時のためにコメントアウト）
# load_dotenv()

# 環境変数からLINEとGeminiのAPIキーを取得
# Renderに設定されている環境変数名に合わせて修正
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

# プロンプトを「役職者お悩みサポート」向けに調整
MANAGEMENT_SUPPORT_SYSTEM_PROMPT = """
あなたは障害福祉施設で働くサービス管理責任者、主任といった管理職・プレイングマネージャーの皆様をサポートするAIであり、「役職者お悩みサポート」という名前です。
日々の業務における幅広い悩みに寄り添い、親身に話を受け止め、ねぎらいの言葉をかけながら、実践的かつ適切なアドバイスを提供してください。

あなたの役割は、組織運営、人材育成、利用者対応、事業展開など、多岐にわたる管理職の悩みを深く理解し、具体的な解決策のヒントや、視点転換のきっかけを提供することです。

【サポートの対象となる悩み（例）】
* 事業所としての組織運営：利用者確保、競合他社との差別化、部下職員の教育・育成、シフト作成、勤怠管理、職員への支援助言・指導
* 利用者様の緊急対応やトラブル対応、クレーム対応
* 虐待、ハラスメント対策
* 今後の事業展開、法令遵守

【回答条件】
* **傾聴と共感**: ユーザーの悩みを頭ごなしに否定せず、まずはその困難な状況や感情に寄り添い、「大変でしたね」「お疲れ様です」といったねぎらいの言葉を必ず含んでください。
* **実践的アドバイス**: 抽象的な精神論ではなく、管理職が現場で具体的に活かせるようなアドバイスを2〜3点、簡潔に提示してください。
* **多角的視点**: 問題解決だけでなく、予防策、リスク管理、部下への影響、他事業所の事例、関連法規、助成金などの多角的な視点からの示唆を与えてください。
* **人材育成の視点**: 部下職員の育成に関する悩みに対しては、具体的なコミュニケーション方法、OJTの進め方、モチベーション向上策などを提案してください。
* **表現の配慮**: 専門用語は避け、平易な言葉で説明してください。
* **長さの調整**: 返答は長すぎず、要点が分かりやすく、かつ情報量が不足しない適切な長さに調整してください。
* **対話の継続**: 各応答の最後に、ユーザーがさらに深掘りして相談できるような、関連性のある問いかけや、次のアクションを促す言葉を必ず含めてください。例：「この件について、他に気になる点はありますか？」「次にどのようなアクションを検討されますか？」「職員の皆様と共有する際に、特に重要だと思う点は何でしょうか？」
* **AIの限界の明示**: AIは個別の事案に対する最終判断や、法律解釈、医療行為、詳細な人事評価など、専門的な判断を伴う領域については直接的なアドバイスはできません。「最終的な判断は貴事業所の状況や専門家の意見に基づいて行ってください」といった旨を適宜伝えてください。緊急を要する、あるいは専門的な判断が必要な場合は、「この内容については、より詳細な情報が必要なため、法人本部や専門家、関係機関にご相談ください。」と案内し、適切な窓口への問い合わせを促してください。

**Gemini APIの無料枠を考慮し、無駄なトークン消費を避けるため、簡潔かつ的確な応答を心がけてください。また、同じような質問の繰り返しは避け、会話の進展を促してください。**
"""

# 初期メッセージ
INITIAL_MESSAGE = "「役職者お悩みサポート」へようこそ。\n日々の事業所運営、職員の育成、利用者様への支援など、多岐にわたるお仕事、本当にお疲れ様です。\n管理職としての重責を担う中で、お一人で抱え込まず、どんな些細なことでも構いませんので、今お悩みのことを気軽にご相談ください。私が羅針盤となり、最適な方向性を見つけるお手伝いをいたします。"

# Gemini API利用制限時のメッセージ
GEMINI_LIMIT_MESSAGE = (
    "申し訳ありません、本日のAIサポートのご利用回数の上限に達しました。\n"
    "明日またお話できますので、その時まで少しお仕事から離れて休憩されてくださいね。\n\n"
    "もし緊急を要するご質問や、詳細な情報が必要な場合は、法人本部や関係機関にご相談ください。"
)

# 過去の会話履歴をGeminiに渡す最大ターン数
MAX_CONTEXT_TURNS = 6 # (ユーザーの発言 + AIの返答) の合計ターン数、トークン消費と相談して調整

# ユーザーごとのセッション情報を保持する辞書
# !!! 重要: 本番環境では、この方法は推奨されません。
# Flaskアプリケーションは、再起動（デプロイ、エラー、Renderのスピンダウンなど）のたびにメモリがリセットされ、
# user_sessions のデータが失われます。
# 会話履歴の永続化には、RenderのPostgreSQL, Redis, Google Cloud Firestore, AWS DynamoDBなどの
# 永続的なデータストアを利用することを強く推奨します。
user_sessions = {}

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)

    if not signature:
        app.logger.error("X-Line-Signature header is missing.")
        abort(400) # 署名がない場合は不正なリクエストとして処理

    app.logger.info("Received Webhook Request:")
    app.logger.info("  Request body (truncated to 500 chars): " + body[:500])
    app.logger.info(f"  X-Line-Signature: {signature}")

    # --- 署名検証のデバッグログ ---
    # ユーザーが提供したコードを保持し、デバッグの助けとなるように残す
    try:
        secret_bytes = CHANNEL_SECRET.encode('utf-8')
        body_bytes = body.encode('utf-8')
        hash_value = hmac.new(secret_bytes, body_bytes, hashlib.sha256).digest()
        calculated_signature = base64.b64encode(hash_value).decode('utf-8')

        app.logger.info(f"  Calculated signature (manual): {calculated_signature}")
        app.logger.info(f"  Channel Secret used for manual calc (first 5 chars): {CHANNEL_SECRET[:5]}...")

        if calculated_signature != signature:
            app.logger.error("!!! Manual Signature MISMATCH detected !!!")
            app.logger.error(f"    Calculated: {calculated_signature}")
            app.logger.error(f"    Received:    {signature}")
            # 手動計算で不一致が検出された場合は、SDK処理に入る前に終了
            abort(400)
        else:
            app.logger.info("  Manual signature check: Signatures match! Proceeding to SDK handler.")

    except Exception as e:
        app.logger.error(f"Error during manual signature calculation for debug: {e}", exc_info=True)
        # 手動計算でエラーが発生しても、SDKの処理は試みる
        pass

    # --- LINE Bot SDKによる署名検証とイベント処理 ---
    try:
        handler.handle(body, signature)
        app.logger.info("Webhook handled successfully by SDK.")
    except InvalidSignatureError:
        app.logger.error("!!! SDK detected Invalid signature !!!")
        app.logger.error("  This typically means CHANNEL_SECRET in Render does not match LINE Developers.")
        app.logger.error(f"  Body (truncated for error log): {body[:200]}...")
        app.logger.error(f"  Signature sent to SDK: {signature}")
        app.logger.error(f"  Channel Secret configured for SDK (first 5 chars): {CHANNEL_SECRET[:5]}...")
        abort(400) # 署名エラーの場合は400を返す
    except Exception as e:
        # その他の予期せぬエラー
        logging.critical(f"Unhandled error during webhook processing by SDK: {e}", exc_info=True)
        abort(500)

    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id # ユーザーIDを取得
    user_message = event.message.text
    app.logger.info(f"Received text message from user_id: '{user_id}', message: '{user_message}' (Reply Token: {event.reply_token})")

    response_text = "申し訳ありません、現在メッセージを処理できません。しばらくしてからもう一度お試しください。"

    # ユーザーセッションの初期化または取得
    current_date = datetime.date.today()

    # 新規ユーザーまたはセッションリセットのロジック
    # (注意: user_sessionsはサーバーの再起動でリセットされます)
    if user_id not in user_sessions or user_sessions[user_id]['last_request_date'] != current_date:
        # 日付が変わった場合、または新規ユーザーの場合、セッションをリセット
        user_sessions[user_id] = {
            'history': [], # 会話履歴は空で開始
            'request_count': 0,
            'last_request_date': current_date
        }
        app.logger.info(f"Initialized/Reset session for user_id: {user_id}. First message of the day or new user.")

        # ユーザー名を取得し、初回メッセージをパーソナライズ
        user_display_name = "管理者" # デフォルト値を「管理者」に変更
        try:
            profile_response = line_bot_api.get_profile(user_id)
            if profile_response and hasattr(profile_response, 'display_name'):
                user_display_name = profile_response.display_name
                app.logger.info(f"Fetched display name for user {user_id}: {user_display_name}")
            else:
                app.logger.warning(f"Could not get display name for user {user_id}. Profile response: {profile_response}")
        except LineBotApiError as e: # LINE APIからのエラーを具体的にキャッチ
            app.logger.error(f"LineBotApiError getting user profile for {user_id}: {e}", exc_info=True)
            # エラー時もデフォルト名で続行
        except Exception as e: # その他の予期せぬエラー
            app.logger.error(f"Unexpected error getting user profile for {user_id}: {e}", exc_info=True)
            # エラー時もデフォルト名で続行

        # パーソナライズされた初期メッセージを生成
        personalized_initial_message = (
            f"{user_display_name}さん、" + INITIAL_MESSAGE
        )
        response_text = personalized_initial_message

        try:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[LineReplyTextMessage(text=response_text)]
                )
            )
            app.logger.info(f"Sent personalized initial message/daily reset message to user {user_id}.")
        except Exception as e:
            logging.error(f"Error sending personalized initial/reset reply to LINE for user {user_id}: {e}", exc_info=True)
        return 'OK' # 初回メッセージ送信後はここで処理を終了。この返信はGeminiを呼び出さない。

    # Gemini API利用回数制限のチェック
    if user_sessions[user_id]['request_count'] >= MAX_GEMINI_REQUESTS_PER_DAY:
        response_text = GEMINI_LIMIT_MESSAGE
        app.logger.warning(f"User {user_id} exceeded daily Gemini request limit ({MAX_GEMINI_REQUESTS_PER_DAY}).")
        try:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[LineReplyTextMessage(text=response_text)]
                )
            )
            app.logger.info(f"Sent limit message to LINE for user {user_id}.")
        except Exception as e:
            logging.error(f"Error sending limit reply to LINE for user {user_id}: {e}", exc_info=True)
        return 'OK'

    # 会話履歴を準備
    # システムプロンプトと初期応答を履歴の最初に含める
    chat_history_for_gemini = [
        {'role': 'user', 'parts': [{'text': MANAGEMENT_SUPPORT_SYSTEM_PROMPT}]},
        {'role': 'model', 'parts': [{'text': "はい、承知いたしました。役職者お悩みサポートとして、ご質問にお答えします。"}]}
    ]

    # MAX_CONTEXT_TURNS に基づいて過去の会話を結合
    # 各ターンはユーザーとモデルのペアなので、履歴から取得する要素数は MAX_CONTEXT_TURNS * 2
    start_index = max(0, len(user_sessions[user_id]['history']) - MAX_CONTEXT_TURNS * 2)

    app.logger.debug(f"Current history length for user {user_id}: {len(user_sessions[user_id]['history'])}. Taking from index {start_index}.")

    # 過去の会話履歴を追加
    for role, text_content in user_sessions[user_id]['history'][start_index:]:
        chat_history_for_gemini.append({'role': role, 'parts': [{'text': text_content}]})

    app.logger.debug(f"Gemini chat history prepared for user {user_id} (last message: '{user_message}'): {chat_history_for_gemini}")

    try:
        # Geminiとのチャットセッションを開始
        # historyにこれまでの会話履歴（システムプロンプト含む）を渡し、
        # 最新のユーザーメッセージのみをsend_messageで送る
        convo = gemini_model.start_chat(history=chat_history_for_gemini)
        gemini_response = convo.send_message(user_message)

        if gemini_response and hasattr(gemini_response, 'text'):
            response_text = gemini_response.text
        elif isinstance(gemini_response, list) and gemini_response and hasattr(gemini_response[0], 'text'):
            response_text = gemini_response[0].text
        else:
            logging.warning(f"Unexpected Gemini response format or no text content: {gemini_response}")
            response_text = "Geminiからの応答形式が予期せぬものでした。"

        app.logger.info(f"Gemini generated response for user {user_id}: '{response_text}'")

        # 会話履歴を更新 (user_sessionsに保存)
        user_sessions[user_id]['history'].append(['user', user_message])
        user_sessions[user_id]['history'].append(['model', response_text])

        # リクエスト数をインクリメント
        user_sessions[user_id]['request_count'] += 1
        user_sessions[user_id]['last_request_date'] = current_date # リクエスト日を更新
        app.logger.info(f"User {user_id} - Request count: {user_sessions[user_id]['request_count']}")

        # オプション：応答の遅延が必要な場合はここに time.sleep を入れる
        # 例: time.sleep(random.uniform(1.0, 3.0))

    except Exception as e:
        logging.error(f"Error interacting with Gemini API for user {user_id}: {e}", exc_info=True)
        response_text = "Geminiとの通信中にエラーが発生しました。時間を置いてお試しください。"

    finally:
        try:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[LineReplyTextMessage(text=response_text)]
                )
            )
            app.logger.info(f"Reply sent to LINE successfully for user {user_id}.")
        except Exception as e:
            logging.error(f"Error replying to LINE for user {user_id}: {e}", exc_info=True)

    return 'OK'

if __name__ == "__main__":
    # Render環境ではPORT環境変数が設定されるため、それを使用する
    # ローカル実行時にはデフォルトで8080を使用
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)