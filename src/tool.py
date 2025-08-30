import os
import requests
from dotenv import load_dotenv
from qwen_agent.agents import Assistant
from qwen_agent.tools.base import BaseTool
from qwen_agent.tools.retrieval import Retrieval
from tavily import TavilyClient
from sqlalchemy import text, create_engine
import smtplib
import psycopg
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

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
# Tool 1: DataAgencyAgent
# -------------------------------
class DataAgencyAgent(BaseTool):
    name: str = "get_data"
    description: str = "Get the earthquake data from BMKG and upsert into DB"

    def __init__(self):
        self.engine = engine
        self.reported_by = 2

    def call(self, params: dict, **kwargs):
        url = "https://data.bmkg.go.id/DataMKG/TEWS/gempadirasakan.json"
        response = requests.get(url, timeout=10)

        if response.status_code != 200:
            return {"error": f"Failed. Status {response.status_code}"}

        try:
            data = response.json()
            earthquakes = data["Infogempa"]["gempa"][:1]

            result = []
            with self.engine.begin() as conn:
                for eq in earthquakes:
                    tanggal = eq.get("Tanggal")
                    jam = eq.get("Jam")

                    try:
                        event_time = datetime.strptime(jam.split(" ")[0], "%H:%M:%S").time()
                    except Exception:
                        event_time = None

                    magnitude = float(eq.get("Magnitude", 0.0))
                    depth = float(eq.get("Kedalaman", "0").replace(" km", ""))

                    conn.execute(
                        text("""
                           INSERT INTO disaster (
                                event_date, event_time, coordinates,
                                magnitude, depth, area, tsunami_potential, reported_by
                            ) VALUES (
                                :event_date, :event_time, :coordinates,
                                :magnitude, :depth, :area, :tsunami_potential, :reported_by
                            )
                            ON CONFLICT (event_date, event_time, coordinates)
                            DO UPDATE SET
                                magnitude = EXCLUDED.magnitude,
                                depth = EXCLUDED.depth,
                                area = EXCLUDED.area,
                                tsunami_potential = EXCLUDED.tsunami_potential,
                                reported_by = EXCLUDED.reported_by;
                        """),
                        {
                            "event_date": tanggal,
                            "event_time": event_time,
                            "coordinates": eq.get("Coordinates"),
                            "magnitude": magnitude,
                            "depth": depth,
                            "area": eq.get("Wilayah"),
                            "tsunami_potential": "Yes" if "TSUNAMI" in eq.get("Potensi", "").upper() else "No",
                            "reported_by": self.reported_by,
                        }
                    )

                    result.append({
                        "Tanggal": tanggal,
                        "Jam": jam,
                        "Coordinates": eq.get("Coordinates"),
                        "Magnitude": eq.get("Magnitude"),
                        "Kedalaman": eq.get("Kedalaman"),
                        "Wilayah": eq.get("Wilayah"),
                        "Potensi": eq.get("Potensi"),
                    })

            return result

        except ValueError:
            return {"error": "JSON is not valid"}


# -------------------------------
# Tool 2: NewsAgent
# -------------------------------
class NewsAgent(BaseTool):
    name = "get_news"
    description = "Get news about earthquake in Indonesia"

    def call(self, params: str, **kwargs):
        tavily_client = TavilyClient(api_key=os.getenv('TAVILY_API_KEY'))
        response = tavily_client.search(
            query="Gempa Hari Ini",
            max_result='5',
            country='indonesia'
        )
        return response
    
# -------------------------------
# Tool 3: QueryDatabaseAgent
# -------------------------------
class QueryDatabaseAgent(BaseTool):
    name = "query_database"
    description = "Get the data from database"

    def call(self, params: str, **kwargs):
        with engine.connect() as conn:
            table_names = conn.execute(text(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public';"
            )).fetchall()
            table_names = [t[0] for t in table_names]
        
        table_list = ", ".join(table_names)
        
        
        system_prompt = f"""
            Kamu adalah QueryDatabaseAgent,
            tugasmu untuk membuat query berdasarkan informasi tabel dari database yang disediakan.

            Berikut isi tabel pada database: {table_list}

            Instruksi:
            1. Buatkan query SQL berdasarkan request {params}.
            2. Gunakan nama tabel yang terdapat pada {table_list} & jangan membuat tabel/kolom baru.
            3. Jawaban hanya dalam format SQL. JANGAN MENAMBAHKAN TEKS.
            Contoh output yang benar:
            SELECT * FROM disaster;
            """

        bot_for_citizen = Assistant(
            llm=llm_cfg,
            system_message=system_prompt
        )

        messages = [{'role': 'user', 
                     'content': f'Jawab hanya querynya saja.'
        }]

        responses = []
        for rsp in bot_for_citizen.run(messages=messages):
            responses.append(rsp)

        last_rsp = responses[-1]
        if isinstance(last_rsp, list):
            for item in reversed(last_rsp):
                if 'content' in item and item['content'].strip():
                    sql_query = item['content'].strip()
                    break
        elif isinstance(last_rsp, dict):
            sql_query = last_rsp.get('content', '').strip()
        else:
            sql_query = ""
    
        with engine.connect() as conn:
            result = conn.execute(text(sql_query)).fetchall()
        data = [tuple(row) for row in result]

        return data

# -------------------------------
# Tool 4: AlertCitizenAgent
# -------------------------------
class AlertCitizenAgent(BaseTool):
    name = "alert_to_citizen"
    description = "Give the personalized alert for citizen based on database"

    def call(self, params: str, **kwargs):

        earthquake_data = DataAgencyAgent().call("Dapatkan data paling terbaru dari gempa bumi")
        citizen_data_raw = QueryDatabaseAgent().call("Tampilkan data email dan name dari tabel citizen")
        citizen_dict = {email: name for email, name in citizen_data_raw}
        citizen_emails = list(citizen_dict.keys())
        
        system_message = """
            Anda adalah AlertCitizenAgent, 
            agent yang bertugas untuk mengirimkan informasi gempa bumi secara real-time kepada warga.

            Tersedia 2 agent pendukung:
            1. DataAgencyAgent: Mengambil data gempa terkini dari sumber resmi.
            2. QueryDatabaseAgent: Mengambil data citizen maupun org untuk pengiriman alert.

            Instruksi:
            1. Baca permintaan user dengan seksama.
            2. Panggil DataAgencyAgent untuk mendapatkan data gempa terkini.
            3. Setelah DataAgencyAgent memberikan data, gunakan data tersebut untuk membuat peringatan lengkap bagi warga.
            4. Panggil QueryDatabaseAgent untuk mengambil data warga.
            5. Peringatan harus jelas, singkat, empati, dan mudah dipahami oleh masyarakat.

            Aturan:
            - Prioritaskan akurasi informasi.
            - Jangan memberikan prediksi gempa yang tidak didukung data tidak resmi.
            - Selalu sertakan sumber informasi.
        """
        
        bot_for_citizen = Assistant(
            llm=llm_cfg,
            system_message = system_message
        )

        personalized_email = []
        for email in citizen_emails:
            name = citizen_dict[email]
            messages = [{'role': 'user',
                         'content': f'Buatkan alert singkat (maksimal 50 kata) gempa personalized untuk {name} dengan data {earthquake_data}'}]
            responses = list(bot_for_citizen.run(messages=messages))
            alert_text = responses[-1]
            personalized_email.append((email, alert_text))

        personalized_email_clean = []
        for item in personalized_email:
            email = item[0]
            response_list = item[1]
            alert_text = response_list[-1]['content']
            personalized_email_clean.append((email, alert_text))

        sender_email = "bashirhanafii00@gmail.com"
        password = "wjay ziej lgmx ugxt"

        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(sender_email, password)

            for recipient, alert_text in personalized_email_clean:
                message = MIMEMultipart()
                message['From'] = sender_email
                message['To'] = recipient
                message['Subject'] = "PERINGATAN GEMPA BUMI"

                message.attach(MIMEText(str(alert_text), 'plain', 'utf-8'))

                server.send_message(message)
                print(f"Email terkirim ke {recipient}")

        return personalized_email

# -------------------------------
# Tool 5: ReportDisasterAgent
# -------------------------------
class ReportDisasterAgent(BaseTool):
    name = "report_disaster"
    description = "Give the alert & information of earthquake impact"

    org_data_raw = QueryDatabaseAgent().call("Tampilkan data email dan name dari tabel org")
    org_dict = {email: name for email, name in org_data_raw}
    org_emails = list(org_dict.keys())
    
    def call(self, params: str, **kwargs):
        earthquake_data = DataAgencyAgent().call("Dapatkan data paling terbaru dari gempa bumi")
        org_data_raw = QueryDatabaseAgent().call("Tampilkan data email dan name dari tabel org")
        org_dict = {email: name for email, name in org_data_raw}
        org_emails = list(org_dict.keys())

        rag = Retrieval(cfg={
            'max_ref_token': 1024,
            'parser_page_size': 500
            }
        )

        rag_results = rag.call({
            "query": "gempa",
            "files": ["/doc/buku-saku-bencana.pdf"]
        })

        system_message = """
            Anda adalah ReportDisasterAgent, 
            agent yang bertugas untuk mengirimkan informasi gempa bumi kepada organisasi atau pemerintah.

            Tersedia 3 agent pendukung:
            1. DataAgencyAgent: Mengambil data gempa terkini dari sumber resmi.
            2. NewsAgent: Mencari dan merangkum berita terbaru terkait gempa.
            3. QueryDatabaseAgent: Mengambil data organisasi dari database.

            Instruksi:
            1. Baca permintaan user dengan seksama.
            2. Panggil DataAgencyAgent untuk mendapatkan data gempa terkini.
            3. Panggil NewsAgent untuk mendapatkan informasi dampak gempa di masyarakat.
            4. Panggil QueryDatabaseAgent untuk mengambil data organisasi.
            5. Gabungkan semua informasi yang dikumpulkan menjadi laporan yang ringkas dan dapat langsung digunakan oleh organisasi kebencanaan.
            6. Laporan harus mencakup:
               - Data gempa resmi
               - Dampak pada masyarakat (kerusakan, korban, wilayah terdampak)
               - Rekomendasi tindakan tanggap darurat untuk organisasi kebencanaan.
            
             Aturan:
            - Prioritaskan akurasi informasi.
            - Jangan memberikan prediksi gempa yang tidak didukung data tidak resmi.
            - Selalu sertakan sumber informasi.
        """
        
        bot_for_organization = Assistant(
            llm=llm_cfg,
            system_message = system_message
        )

        personalized_email = []
        for email in org_emails:
            name = org_dict[email]
            messages = [{'role': 'user',
                         'content': f'Buatkan laporan singkat (maksimal 200 kata) gempa personalized untuk organisasi {name} dengan data {earthquake_data}.\n'
                         f'Buku saku bencana: {rag_results}'}
                       ]
            responses = list(bot_for_organization.run(messages=messages))
            alert_text = responses[-1]
            personalized_email.append((email, alert_text))

        personalized_email_clean = []
        for item in personalized_email:
            email = item[0]
            response_list = item[1]
            alert_text = response_list[-1]['content']
            personalized_email_clean.append((email, alert_text))

        sender_email = "bashirhanafii00@gmail.com"
        password = "wjay ziej lgmx ugxt"

        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(sender_email, password)

            for recipient, report_text in personalized_email_clean:
                message = MIMEMultipart()
                message['From'] = sender_email
                message['To'] = recipient
                message['Subject'] = "LAPORAN GEMPA BUMI"

                message.attach(MIMEText(str(report_text), 'plain', 'utf-8'))

                server.send_message(message)
                print(f"Email terkirim ke {recipient}")

        return personalized_email
