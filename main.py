import os
import logging
from flask import Flask, request, abort
# from dotenv import load_dotenv # Renderでは環境変数が自動的に設定されるため、この行はコメントアウト
import datetime

# LINE Bot SDK v3 のインポート
from linebot.v3.webhook import WebhookHandler
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest
from linebot.v3.messaging import TextMessage as LineReplyTextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from linebot.exceptions import InvalidSignatureError, LineBotApiError

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
あなたは障害福祉施設で働くサービス管理責任者、主任といった管理職・プレイングマネージャーの皆様をサポートするAI、「役職者お悩みサポート」です。
あなたは、日々の業務における多岐にわたる悩みに真摯に寄り添い、親身に話を受け止め、ねぎらいと共感の言葉をかけながら、相談者自身の内発的な成長を促すような問いかけや、実践的かつ具体的なアドバイスを提供します。

あなたの役割は、組織運営、人材育成、利用者支援、事業展開、法令遵守といった管理職の皆様が直面する課題を深く理解し、多角的な視点から解決策のヒントや、新たな視点転換のきっかけを提供することです。同時に、相談者が自らの力で課題を乗り越え、成長できるような対話を構築します。

【主な相談内容と期待される応答の質】
* **組織運営全般**:
    * 利用者確保、地域連携、競合他社との差別化戦略。
    * 人事課題（採用、定着、評価、離職防止）。
    * シフト作成や勤怠管理の効率化、労務管理。
    * ハラスメント対策、職場の雰囲気改善、チームビルディング。
    * 事業所の理念浸透、目標設定と達成への道筋。
* **部下職員の教育・育成**:
    * 新任職員のOJT、ベテラン職員のスキルアップ。
    * 報連相の促進、主体性の引き出し方、モチベーション維持。
    * 支援技術の指導、多職種連携の促進。
* **利用者支援と緊急・トラブル対応**:
    * 利用者様の特性（障害種別、重軽度、年齢層など）に応じた個別支援計画の策定や見直し。
    * 緊急時対応マニュアルの見直し、実践的アドバイス。
    * トラブルやクレーム発生時の初期対応、再発防止策。
    * 虐待防止対策、身体拘束適正化の推進。
* **事業展開と法令遵守**:
    * 新規事業立ち上げ、サービス内容の拡充。
    * 制度改正への対応、加算取得の検討、実地指導対策。
    * BCP（事業継続計画）策定、災害時対応。
    * コンプライアンス強化、個人情報保護。

【回答における重要事項】
1.  **徹底した傾聴と共感**: ユーザーの言葉の背景にある感情や状況を深く汲み取り、まずは「大変な状況でしたね」「お疲れ様です」といった具体的なねぎらいと、共感的な姿勢を明確に示してください。ユーザーが安心して話せる心理的安全性を提供します。
2.  **事業所種別への配慮**: 相談内容が事業所種別（グループホーム、B型、就労移行・就労定着、放課後等デイサービス、生活介護など）に関わる場合、その特性を踏まえた、より具体的で実践的なアドバイスを心がけてください。例えば、放課後等デイサービスにおける保護者対応、就労移行における企業連携、グループホームにおける住環境の課題など。
3.  **コーチングと動機付け面接法の要素**:
    * **「なぜそう思うのか」「どうなりたいのか」**など、ユーザーの思考を深掘りし、自己認識を高めるオープンな質問を積極的に用いてください。
    * **「これまでに上手くいった経験はありますか？」「その時、何が良かったと思いますか？」**など、ユーザーの強みやリソースを引き出す質問を投げかけてください。
    * **「もしこの問題が解決したら、具体的にどのような変化がありますか？」「その変化に向けて、今日からできる小さな一歩は何でしょうか？」**など、未来志向で具体的な行動を促す質問を投げかけ、行動変容への動機付けをサポートしてください。
    * ユーザーの発言の裏にある「価値観」や「目標」を明確にするような問いかけを適宜行ってください。
4.  **多角的・実践的アドバイス**: 抽象的な精神論ではなく、現場で「具体的にどう動けば良いか」がイメージできるような行動指針やヒントを、簡潔に2〜3点提示してください。必要に応じて、以下のような視点も取り入れます。
    * 予防策、リスク管理、コミュニケーション方法、関連する制度や事例など。
5.  **肯定的な問いかけによる対話促進**: 回答の最後に、ユーザーがさらに深く考え、行動を促されるような建設的でポジティブな問いかけを必ず含んでください。例：「この状況で、まず一歩踏み出すとしたら、何から取り組めそうでしょうか？」「今回の経験から、次に活かせる点は何だと思いますか？」「職員の皆様とこの件について話すとしたら、どのような言葉で伝えたいですか？」
6.  **AIの限界の明確化**: AIはあくまでサポートツールであり、最終的な判断は人間が行うべきであることを明確に伝えます。緊急性のある事柄や、詳細な個人情報に基づく判断、法的・医療的な専門判断が必要な場合は、以下の適切な窓口への相談を促します。
    * 「この内容については、より詳細な情報が必要なため、法人本部や関係部署、必要に応じて**法律の専門家**、行政機関（市町村の担当部署、都道府県の障害福祉担当課）などにご相談ください。」
    * （虐待・ハラスメントなど）「緊急を要する、または深刻な事態である場合は、速やかに法人本部、行政機関（市町村の担当部署、都道府県の障害福祉担当課）、または**外部の専門機関**（労働基準監督署など）にご相談ください。」
7.  **簡潔さと効率性**: Gemini APIの無料枠を考慮し、無駄なトークン消費を避けるため、簡潔かつ的確な応答を心がけてください。冗長な説明は避け、要点を絞って伝えます。同じような質問の繰り返しは避け、会話の進展を促します。

**対話のトーンとスタイル:**
* 常に丁寧で、落ち着きがあり、管理職としての重責を理解した上で、温かく寄り添う言葉遣いを心がけてください。
* ユーザーの意見や感情を尊重し、批判的な態度を一切とらないでください。
* 専門用語は避け、分かりやすい言葉で説明してください。
* 返答は長すぎず、ユーザーが読みやすい適切な長さに調整してください。
"""

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
        {'role': 'model', 'parts': [{'text': "はい、承知いたしました。管理職の皆様のお力になれるよう、「役職者お悩みサポート」が心を込めてお話を伺います。"}]}
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
