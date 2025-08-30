import os
from qwen_agent.agents import Assistant
import time
from qwen_agent.gui import WebUI
from tool import DataAgencyAgent, QueryDatabaseAgent, NewsAgent, AlertCitizenAgent, ReportDisasterAgent
from sqlalchemy import create_engine
import psycopg
from dotenv import load_dotenv

# -------------------------------
# Database
# -------------------------------
load_dotenv()
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")

engine = create_engine(
    f"postgresql+psycopg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)
conn_info = f"dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD} host={DB_HOST} port={DB_PORT}"
sync_connection = psycopg.connect(conn_info)

# -------------------------------
# Coordinator Agent
# -------------------------------
system_prompt = f"""
    Kamu adalah CoordinatorAgent yang bertindak untuk memutuskan agent
    mana yang paling tepat untuk menangani permintaan user.

    Tersedia 5 agent:
    1. DataAgencyAgent: Mengambil data gempa terkini dari sumber resmi.
    2. NewsAgent: Mencari dan merangkum berita terbaru terkait gempa.
    3. AlertCitizenAgent: Membuat pesan peringatan untuk warga jika terjadi gempa.
       - AlertCitizenAgent harus memanggil DataAgencyAgent sebelum membuat peringatan.
       - AlertCitizenAgent harus memanggil QueryDatabaseAgent untuk memanggil data email dari tabel citizen.
    4. ReportDisasterAgent: Membuat laporan bencana alam kepada organisasi jika terjadi gempa.
       - ReportDisasterAgent harus memanggil DataAgencyAgent dan NewsAgent sebelum membuat laporan.
       - ReportDisasterAgent harus memanggil QueryDatabaseAgent untuk memanggil data email dari tabel citizen.
    5. QueryDatabaseAgent: Memanggil database dari internal

    Instruksi:
    - Analisis perintah dari user.
    - Tentukan agent yang paling relevan.
    - Balas dengan bahasa yang sopan, ringkas, dan menenangkan.
    - Jika user menyapa, balas dengan sopan dan tawarkan bantuan mengenai informasi gempa.
"""

# model
llm_cfg = {
    'model': 'vllm-qwen3',
    'model_server': 'https://litellm.bangka.productionready.xyz/',
    'api_key': os.getenv('API_KEY'),
    'generate_cfg': {
        'top_p': 0.1,
        'temperature': 0.1
    }
}

# -------------------------------
# Agents
# -------------------------------
data_agent = DataAgencyAgent()
news_agent = NewsAgent()
query_agent = QueryDatabaseAgent()
alert_agent = AlertCitizenAgent()
report_agent = ReportDisasterAgent()
tools = [data_agent, news_agent, query_agent, alert_agent, report_agent]

# -------------------------------
# Bot
# -------------------------------
bot = Assistant(llm=llm_cfg, 
                function_list=tools)

# -------------------------------
# Mode
# -------------------------------
last_event_id = None
def check_and_alert():
    global last_event_id

    earthquake_data = DataAgencyAgent().call({})
    if not earthquake_data:
        print("Tidak ada data gempa terbaru")
        return
    
    eq = earthquake_data[0]
    event_id = eq["Tanggal"] + eq["Jam"] + eq["Coordinates"]

    if event_id != last_event_id:
        last_event_id = event_id
        print("Gempa baru terdeteksi!")
        AlertCitizenAgent().call("Buatkan peringatan gempa personalized ke citizen yang terdaftar")
        ReportDisasterAgent().call("Buatkan report gempa personalized ke organisasi yang terdaftar")
    else:
        print("Tidak ada gempa baru")

def run_realtime():
    while True:
        check_and_alert()
        # repeat in 30s
        time.sleep(30)

# Chatbot UI
def run_chatbot():
    WebUI(bot).run()
    WebUI(bot).launch(share=True)

# Select mode
if __name__ == "__main__":
    print("SELECT MODE")
    print("1. Real-time Alert")
    print("2. Chatbot UI")
    choice = input("Select (1/2): ").strip()

    if choice == "1":
        run_realtime()
    elif choice == "2":
        run_chatbot()
    else:
        print("Input isn't valid!")



