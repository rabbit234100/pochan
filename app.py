import os
os.system("playwright install chromium") # 앱 실행 시 크롬 브라우저 자동 설치

import streamlit as st
import time
import sqlite3
import json
from datetime import datetime
from bs4 import BeautifulSoup
from pydantic import BaseModel
from typing import List, Literal, Optional
import google.generativeai as genai
from playwright.sync_api import sync_playwright

# --- 1. 데이터 모델 정의 (Pydantic) ---
class PokemonDetail(BaseModel):
    name: str
    is_shiny: bool = False
    ball: Optional[str] = None
    is_genned: bool = False
    is_ot: Optional[bool] = None

class GiveawayRecipient(BaseModel):
    username: str
    received_pokemon: List[PokemonDetail]
    status: Literal["COMPLETED", "PENDING", "NO_SHOW"]

class GiveawayExtraction(BaseModel):
    host_username: str
    recipients: List[GiveawayRecipient]

# --- 2. 데이터베이스 초기화 및 함수 ---
def get_db_connection():
    conn = sqlite3.connect('giveaway_logs_v2.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            status TEXT,
            data TEXT,
            created_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- 3. 핵심 기능 함수 (크롤링 및 추출) ---
def scrape_post(url: str, user_cookies: dict = None) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--window-size=1920,1080"
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        
        # 💡 필수 쿠키 3종 세트 즉시 주입 (Cloudflare 우회 핵심)
        if user_cookies and user_cookies.get("session"):
            context.add_cookies([
                {"name": "arca.session2", "value": user_cookies["session"].strip(), "domain": ".arca.live", "path": "/"},
                {"name": "arca.session2.sig", "value": user_cookies.get("sig", "").strip(), "domain": ".arca.live", "path": "/"},
                {"name": "cf_clearance", "value": user_cookies.get("cf", "").strip(), "domain": ".arca.live", "path": "/"}
            ])
            
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            time.sleep(3) # Cloudflare 대기
            
            html = page.content()
            soup = BeautifulSoup(html, 'html.parser')
            
            main_content = soup.select_one('.article-wrapper')
            comments = soup.select_one('.list-area')
            
            text_content = ""
            if main_content:
                text_content += "[본문]\n" + main_content.get_text(separator='\n', strip=True) + "\n\n"
            if comments:
                text_content += "[댓글]\n" + comments.get_text(separator='\n', strip=True)
                
            if not text_content:
                text_content = soup.get_text(separator='\n', strip=True)
                
            return text_content[:5000]
        except Exception as e:
            return f"Error: {str(e)}"
        finally:
            browser.close()

def extract_giveaway_data(text: str, api_key: str) -> GiveawayExtraction:
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-1.5-flash')
    
    system_prompt = "포켓몬 커뮤니티의 '나눔' 게시글을 분석하여 누가 무엇을 받아갔는지 추출해."
    
    response = model.generate_content(
        f"{system_prompt}\n\n[게시글 내용]\n{text}",
        generation_config=genai.GenerationConfig(
            response_mime_type="application/json",
            response_schema=GiveawayExtraction,
            temperature=0.1
        )
    )
    
    data_dict = json.loads(response.text)
    return GiveawayExtraction(**data_dict)

# --- 4. Streamlit UI 및 Secrets 설정 ---
st.set_page_config(page_title="포켓몬 나눔 아카이브", layout="wide")

try:
    gemini_api_key = st.secrets["GEMINI_API_KEY"]
except KeyError:
    gemini_api_key = None

try:
    c_session = st.secrets.get("COOKIE_SESSION", "")
    c_sig = st.secrets.get("COOKIE_SIG", "")
    c_cf = st.secrets.get("COOKIE_CF", "")
    secret_arca_cookies = {
        "session": c_session,
        "sig": c_sig,
        "cf": c_cf
    }
except Exception:
    secret_arca_cookies = None

with st.sidebar:
    st.header("🔑 메뉴")
    menu = st.radio("이동", ["유저: 나눔 기록하기", "관리자: 통계 및 오류 관리"])
    
    if not gemini_api_key:
        st.error("⚠️ 시스템 오류: 서버(Secrets)에 Gemini API 키가 설정되지 않았습니다.")
    if not secret_arca_cookies or not secret_arca_cookies.get("session"):
        st.warning("⚠️ 시스템 경고: 서버(Secrets)에 COOKIE_SESSION 등 인증 정보가 설정되지 않았습니다. 크롤링이 실패할 확률이 높습니다.")

# ----------------------------------------------------
# [화면 1] 유저용 메인 화면 (즉시 뚫기 + 결과 바로 출력)
# ----------------------------------------------------
if menu == "유저: 나눔 기록하기":
    st.title("🐾 포켓몬 나눔 자동 로거")
    st.markdown("나눔 받은 게시글의 링크를 입력하면 자동으로 분석하여 아카이브에 영구 기록합니다.")
    
    url_input = st.text_input("나눔 게시글 URL", placeholder="https://arca.live/b/pokemon/...")
    
    if st.button("내역 자동 추출하기", type="primary"):
        if not gemini_api_key:
            st.error("서버에 API 키가 설정되지 않아 기능을 사용할 수 없습니다.")
        elif url_input:
            with st.spinner("서버 쿠키를 적용하여 글을 읽고 분석 중입니다... (약 10초 소요)"):
                conn = get_db_connection()
                existing = conn.execute('SELECT * FROM logs WHERE url = ?', (url_input,)).fetchone()
                
                if existing and existing["status"] == "COMPLETED":
                    st.success("이미 분석이 완료되어 저장된 글입니다.")
                    saved_data = GiveawayExtraction(**json.loads(existing["data"]))
                    st.info(f"👑 **주최자:** {saved_data.host_username}")
                    for rec in saved_data.recipients:
                        pkmns = ", ".join([p.name for p in rec.received_pokemon])
                        st.write(f"➡️ **{rec.username}** 님: {pkmns} 수령")
                else:
                    # 💡 유저가 요청하자마자 즉시 서버 쿠키(3종)를 장착하고 긁어옵니다.
                    raw_text = scrape_post(url_input, secret_arca_cookies)
                    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    
                    if "Error" in raw_text or "로그인" in raw_text or "성인" in raw_text:
                        try:
                            conn.execute(
                                'INSERT OR REPLACE INTO logs (url, status, created_at) VALUES (?, ?, ?)', 
                                (url_input, "FAILED", current_time)
                            )
                            conn.commit()
                        except sqlite3.IntegrityError:
                            pass
                        st.error("❌ 크롤링 실패: 삭제된 글이거나 쿠키 만료/봇 차단에 걸렸습니다. (관리자 오류 목록에 등록됨)")
                    else:
                        try:
                            extracted_data = extract_giveaway_data(raw_text, gemini_api_key)
                            conn.execute(
                                'INSERT OR REPLACE INTO logs (url, status, data, created_at) VALUES (?, ?, ?, ?)',
                                (url_input, "COMPLETED", extracted_data.model_dump_json(), current_time)
                            )
                            conn.commit()
                            st.success("✅ 나눔 내역이 성공적으로 추출 및 저장되었습니다!")
                            
                            st.markdown("### 🎁 추출된 나눔 내역")
                            st.info(f"👑 **주최자:** {extracted_data.host_username}")
                            
                            if not extracted_data.recipients:
                                st.write("당첨자 내역을 찾지 못했습니다.")
                            else:
                                for rec in extracted_data.recipients:
                                    pkmns = ", ".join([p.name for p in rec.received_pokemon])
                                    st.write(f"➡️ **{rec.username}** 님: {pkmns} 수령")
                                    
                        except Exception as e:
                            st.error(f"분석 중 오류 발생 (LLM 에러): {e}")
                
                conn.close()

# ----------------------------------------------------
# [화면 2] 관리자 전용 화면 (통계 + 완전히 오류난 링크 조회)
# ----------------------------------------------------
elif menu == "관리자: 통계 및 오류 관리":
    st.title("🛠️ 관리자 대시보드")
    
    admin_pw = st.text_input("관리자 비밀번호", type="password")
    
    if admin_pw == "rabbit777": 
        tab1, tab2 = st.tabs(["📊 유저별 나눔 통계", "❌ 오류 링크 목록 및 관리"])
        
        # --- 탭 1: 나눔 내역 통계 ---
        with tab1:
            st.subheader("📚 유저별 나눔 수령 현황")
            st.caption("※ 동일 날짜 2회 초과(3회 이상), 동일 주간 5회 초과(6회 이상) 수령 유저는 닉네임이 빨간색으로 표시됩니다.")
            
            conn = get_db_connection()
            completed_logs = conn.execute('SELECT * FROM logs WHERE status = "COMPLETED" ORDER BY id DESC').fetchall()
            
            user_stats = {}
            weekdays_ko = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
            
            # 1. 데이터 집계
            for log in completed_logs:
                data = json.loads(log["data"])
                date_str_full = log["created_at"] if log["created_at"] else "2026-01-01 00:00:00"
                
                try:
                    dt = datetime.strptime(date_str_full, "%Y-%m-%d %H:%M:%S")
                except:
                    dt = datetime.now()
                    
                date_key = dt.strftime("%Y-%m-%d")
                week_key = dt.strftime("%Y-%W")
                weekday_name = weekdays_ko[dt.weekday()]
                
                for recipient in data['recipients']:
                    uname = recipient["username"]
                    if uname not in user_stats:
                        user_stats[uname] = {
                            "total": 0, "by_date": {}, "by_week": {}, 
                            "weekdays": {w: 0 for w in weekdays_ko}, "history": []
                        }
                    
                    user_stats[uname]["total"] += 1
                    user_stats[uname]["by_date"][date_key] = user_stats[uname]["by_date"].get(date_key, 0) + 1
                    user_stats[uname]["by_week"][week_key] = user_stats[uname]["by_week"].get(week_key, 0) + 1
                    user_stats[uname]["weekdays"][weekday_name] += 1
                    
                    pkmns = ", ".join([p["name"] for p in recipient["received_pokemon"]])
                    user_stats[uname]["history"].append(f"[{date_key} {weekday_name}] {pkmns} (주최: {data['host_username']}) - 🔗 [링크]({log['url']})")
            
            # 2. 화면 출력 및 규정 검사
            for uname, stats in user_stats.items():
                over_daily = any(count > 2 for count in stats["by_date"].values())
                over_weekly = any(count > 5 for count in stats["by_week"].values())
                
                if over_daily or over_weekly:
                    display_name = f":red[{uname} 🚨 (규정 초과)]"
                else:
                    display_name = f"{uname}"
                    
                with st.expander(f"👤 {display_name} - 총 {stats['total']}회 수령"):
                    weekday_texts = [f"{w} {stats['weekdays'][w]}회" for w in weekdays_ko if stats['weekdays'][w] > 0]
                    st.markdown(f"**[요일별 누적 수령]** {', '.join(weekday_texts)}")
                    st.markdown("**[상세 내역]**")
                    for h in stats["history"]:
                        st.write(f"- {h}")
                        
            if not user_stats:
                st.info("아직 저장된 나눔 완료 내역이 없습니다.")

        # --- 탭 2: 완전히 오류난 링크 조회 ---
        with tab2:
            st.subheader("❌ 크롤링 실패 링크 목록")
            st.caption("서버 쿠키를 적용했는데도 삭제되었거나 읽을 수 없어 실패한 링크들입니다.")
            
            failed_logs = conn.execute('SELECT * FROM logs WHERE status = "FAILED"').fetchall()
            
            if failed_logs:
                for log in failed_logs:
                    st.code(f"[{log['created_at']}] {log['url']}")
                
                st.markdown("---")
                if st.button("오류 목록 비우기"):
                    conn.execute('DELETE FROM logs WHERE status = "FAILED"')
                    conn.commit()
                    st.success("오류 링크 목록이 삭제되었습니다.")
                    time.sleep(1)
                    st.rerun()
            else:
                st.info("현재 오류가 발생한 링크가 없습니다.")
                
            st.markdown("---")
            st.subheader("🚨 전체 데이터 초기화 구역")
            if st.button("모든 나눔 데이터 및 기록 삭제", type="primary"):
                conn.execute('DELETE FROM logs')
                conn.commit()
                st.success("✅ 모든 데이터가 성공적으로 초기화되었습니다.")
                time.sleep(1.5)
                st.rerun()
                
        conn.close()
            
    elif admin_pw:
        st.error("비밀번호가 틀렸습니다.")
