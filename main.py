import os
import logging
from flask import Flask, request, abort
import datetime
import time
import threading # 非同期処理のためにthreadingをインポート

# LINE Bot SDK v3 のインポート
from linebot.v3.webhook import WebhookHandler
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest
from linebot.v3.messaging.models import GetProfileRequest
from linebot.v3.messaging import TextMessage as LineReplyTextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.exceptions import InvalidSignatureError, LineBotApiError

# Google Generative AI SDK のインポート
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# ロギング設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
app = Flask(__name__)

# 環境変数からLINEとGeminiのAPIキーを取得
CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# 環境変数が設定されているか確認
if not CHANNEL_ACCESS_TOKEN:
    logging.critical("LINE_CHANNEL_ACCESS_TOKEN is not set in environment variables.")
    raise ValueError("LINE_CHANNEL_ACCESS_TOKEN is not set. Please set it in Render Environment Variables.")
if not CHANNEL_SECRET:
    logging.critical("LINE_CHANNEL_SECRET is not set in environment variables.")
    raise ValueError("LINE_CHANNEL_SECRET is not set. Please set it in Render Environment Variables.")
if not GEMINI_API_KEY:
    logging.critical("GEMINI_API_KEY is not set in environment variables.")
    raise ValueError("GEMINI_API_KEY is not set. Please set it in Render Environment Variables.")
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
    logging.critical(f"Failed to configure LINE Bot SDK: {e}. Please check LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET.")
    raise Exception(f"LINE Bot SDK configuration failed: {e}")

# Gemini API の設定
try:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel(
        'gemini-2.5-flash-lite-preview-06-17',
        safety_settings={
            HarmCategory.HARMS_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARMS_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARMS_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARMS_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
    )
    logging.info("Gemini API configured successfully using 'gemini-2.5-flash-lite-preview-06-17' model.")
except Exception as e:
    logging.critical(f"Failed to configure Gemini API: {e}. Please check GEMINI_API_KEY and 'google-generativeai' library version in requirements.txt. Also ensure 'gemini-2.5-flash-lite-preview-06-17' model is available for your API Key/Region.")
    raise Exception(f"Gemini API configuration failed: {e}")

# --- チャットボット関連の設定 ---
MAX_GEMINI_REQUESTS_PER_DAY = 20

# プロンプトの簡潔化を適用
MANAGEMENT_SUPPORT_SYSTEM_PROMPT = """
あなたは障害福祉施設の管理職向けAIサポート「役職者お悩みサポート」です。
組織運営、人材育成、利用者支援、事業展開、法令遵守に関する悩みに、傾聴と共感を持ち、実践的かつ具体的なアドバイスを端的に提供してください。
ユーザーの思考を深掘りし、強みと行動を促すオープンな質問を含めてください。
回答の最後に、建設的な質問を必ず含めてください。
専門用語は避け、分かりやすい言葉で説明してください。
AIの限界を認識し、必要に応じ専門家への相談を促してください。
応答は簡潔に、トークン消費を抑え、会話の発展を促すこと。
"""

INITIAL_MESSAGE = """
「役職者お悩みサポート」へようこそ。
日々の事業所運営、職員の育成、利用者様への支援、多岐にわたる管理職のお仕事、本当にお疲れ様です。
どんな些細なことでも構いませんので、今お悩みのことを気軽にご相談ください。
私が、あなたの「相談役」として、最適な方向性を見つけるお手伝いをいたします。
"""

GEMINI_LIMIT_MESSAGE = """
申し訳ありません、本日の「役職者お悩みサポート」のご利用回数の上限に達しました。
日々の激務の中、ご活用いただきありがとうございます。
明日またお話しできますので、その時まで少しお仕事から離れて、ご自身の心身を労わってくださいね。

もし緊急を要するご質問や、詳細な情報が必要な場合は、法人本部や関係部署、関連機関にご相談ください。
明日、またお会いできることを楽しみにしております。
"""

MAX_CONTEXT_TURNS = 6

user_sessions = {}

# LINEへの返信を非同期で行う関数
def deferred_reply(reply_token, messages_to_send, user_id, start_time):
    try:
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=messages_to_send
            )
        )
        app.logger.info(f"[{time.time() - start_time:.3f}s] Deferred reply sent to LINE successfully for user {user_id}.")
    except Exception as e:
        app.logger.error(f"Error sending deferred reply to LINE for user {user_id}: {e}", exc_info=True)

@app.route("/callback", methods=['POST'])
def callback():
    start_callback_time = time.time()
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)

    if not signature:
        app.logger.error(f"[{time.time() - start_callback_time:.3f}s] X-Line-Signature header is missing.")
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
        abort(400)
    except Exception as e:
        logging.critical(f"[{time.time() - start_callback_time:.3f}s] Unhandled error during webhook processing by SDK: {e}", exc_info=True)
        abort(500)

    app.logger.info(f"[{time.time() - start_callback_time:.3f}s] Total callback processing time.")
    return 'OK' # ここで即座にOKを返す

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    start_handle_time = time.time()
    user_id = event.source.user_id
    user_message = event.message.text
    app.logger.info(f"[{time.time() - start_handle_time:.3f}s] handle_message received for user_id: '{user_id}', message: '{user_message}' (Reply Token: {event.reply_token})")

    messages_to_send = []
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

        # ユーザー名取得 (初回のみ) は非同期処理の後で良い、または別スレッドで取得し次回以降のメッセージで反映する
        # この初回メッセージの返信自体は、ユーザープロファイル取得を待たずに即時行う
        personalized_initial_message = INITIAL_MESSAGE
        
        # ユーザー名取得は時間がかかる可能性があるため、非同期処理の恩恵を最大限受けるために、
        # この初期メッセージでは表示しないか、別スレッドで取得して次のメッセージで反映を検討
        # 今回は、初回メッセージで名前を取得するロジックは維持しつつ、非同期処理の外に出すことでWebhookの応答を早くする
        try:
            start_get_profile = time.time()
            profile_response = line_bot_api.get_profile(GetProfileRequest(user_id=user_id))
            if profile_response and hasattr(profile_response, 'display_name'):
                user_sessions[user_id]['display_name'] = profile_response.display_name
                app.logger.info(f"[{time.time() - start_get_profile:.3f}s] Fetched display name for user {user_id}: {user_sessions[user_id]['display_name']}")
                personalized_initial_message = f"{user_sessions[user_id]['display_name']}さん、" + INITIAL_MESSAGE
            else:
                app.logger.warning(f"[{time.time() - start_get_profile:.3f}s] Could not get display name for user {user_id}.")
        except LineBotApiError as e:
            app.logger.error(f"[{time.time() - start_get_profile:.3f}s] LineBotApiError getting user profile for {user_id}: {e}", exc_info=True)
        except Exception as e:
            app.logger.error(f"[{time.time() - start_get_profile:.3f}s] Unexpected error getting user profile for {user_id}: {e}", exc_info=True)

        messages_to_send.append(LineReplyTextMessage(text=personalized_initial_message))
        
        # 非同期でLINEに返信
        threading.Thread(target=deferred_reply, args=(event.reply_token, messages_to_send, user_id, start_handle_time)).start()
        app.logger.info(f"[{time.time() - start_handle_time:.3f}s] handle_message finished for initial/reset flow (deferred reply).")
        return 'OK'

    # Gemini API利用回数制限のチェック
    if user_sessions[user_id]['request_count'] >= MAX_GEMINI_REQUESTS_PER_DAY:
        response_text = GEMINI_LIMIT_MESSAGE
        app.logger.warning(f"User {user_id} exceeded daily Gemini request limit ({MAX_GEMINI_REQUESTS_PER_DAY}).")
        messages_to_send.append(LineReplyTextMessage(text=response_text))

        # 非同期でLINEに返信
        threading.Thread(target=deferred_reply, args=(event.reply_token, messages_to_send, user_id, start_handle_time)).start()
        app.logger.info(f"[{time.time() - start_handle_time:.3f}s] handle_message finished for limit exceeded flow (deferred reply).")
        return 'OK'

    # 会話履歴を準備
    chat_history_for_gemini = [
        {'role': 'user', 'parts': [{'text': MANAGEMENT_SUPPORT_SYSTEM_PROMPT}]},
        {'role': 'model', 'parts': [{'text': "はい、承知いたしました。管理職の皆様のお力になれるよう、「役職者お悩みサポート」が心を込めてお話を伺います。"}]}
    ]

    start_index = max(0, len(user_sessions[user_id]['history']) - MAX_CONTEXT_TURNS * 2)
    app.logger.debug(f"[{time.time() - start_handle_time:.3f}s] Current history length for user {user_id}: {len(user_sessions[user_id]['history'])}. Taking from index {start_index}.")

    for role, text_content in user_sessions[user_id]['history'][start_index:]:
        chat_history_for_gemini.append({'role': role, 'parts': [{'text': text_content}]})

    app.logger.debug(f"[{time.time() - start_handle_time:.3f}s] Gemini chat history prepared for user {user_id} (last message: '{user_message}'): {chat_history_for_gemini}")

    response_text = "申し訳ありません、現在メッセージを処理できません。しばらくしてからもう一度お試しください。" # デフォルトエラーメッセージ

    try:
        start_gemini_call = time.time()
        convo = gemini_model.start_chat(history=chat_history_for_gemini)
        gemini_response = convo.send_message(user_message)
        end_gemini_call = time.time()
        app.logger.info(f"[{end_gemini_call - start_gemini_call:.3f}s] Gemini API call completed for user {user_id}.")

        if gemini_response and hasattr(gemini_response, 'text'):
            response_text = gemini_response.text
        elif isinstance(gemini_response, list) and gemini_response and hasattr(gemini_response[0], 'text'):
            response_text = gemini_response[0].text
        else:
            logging.warning(f"[{time.time() - start_handle_time:.3f}s] Unexpected Gemini response format or no text content: {gemini_response}")
            response_text = "Geminiからの応答形式が予期せぬものでした。"

        app.logger.info(f"[{time.time() - start_handle_time:.3f}s] Gemini generated response for user {user_id}: '{response_text}'")

        # 会話履歴を更新
        user_sessions[user_id]['history'].append(['user', user_message])
        user_sessions[user_id]['history'].append(['model', response_text])
        user_sessions[user_id]['request_count'] += 1
        user_sessions[user_id]['last_request_date'] = current_date
        app.logger.info(f"[{time.time() - start_handle_time:.3f}s] User {user_id} - Request count: {user_sessions[user_id]['request_count']}")

    except Exception as e:
        logging.error(f"[{time.time() - start_handle_time:.3f}s] Error interacting with Gemini API for user {user_id}: {e}", exc_info=True)
        response_text = "Geminiとの通信中にエラーが発生しました。時間を置いてお試しください。"

    finally:
        messages_to_send.append(LineReplyTextMessage(text=response_text))
        # 非同期でLINEに返信
        threading.Thread(target=deferred_reply, args=(event.reply_token, messages_to_send, user_id, start_handle_time)).start()

    app.logger.info(f"[{time.time() - start_handle_time:.3f}s] Total handle_message processing time (deferred reply).")
    return 'OK'

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
