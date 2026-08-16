import os
os.system("playwright install chromium") # 앱 실행 시 크롬 브라우저 자동 설치

import streamlit as st
import time
import sqlite3
import json
from datetime import datetime
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field
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
# 새 버전의 DB 생성 (날짜 기록용 created_at 컬럼 추가)
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
def scrape_post(url: str, user_cookie: str = None) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        
        # 🔑 수정됨: 쿠키 이름이 arca.session2 로 변경되었습니다.
        if user_cookie:
            context.add_cookies([
                {"name": "arca.session2", "value": user_cookie.strip(), "domain": ".arca.live", "path": "/"}
            ])
            
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            time.sleep(3)
            
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

# 🔑 수정됨: Secrets에서 Gemini API 키와 ARCA 쿠키를 모두 불러옵니다.
try:
    gemini_api_key = st.secrets["GEMINI_API_KEY"]
except KeyError:
    gemini_api_key = None

try:
    secret_arca_cookie = st.secrets["ARCA_COOKIE"]
except KeyError:
    secret_arca_cookie = None


with st.sidebar:
    st.header("🔑 메뉴")
    menu = st.radio("이동", ["유저: 나눔 기록하기", "관리자: 일괄 처리 및 통계"])
    
    if not gemini_api_key:
        st.error("⚠️ 시스템 오류: 서버(Secrets)에 Gemini API 키가 설정되지 않았습니다.")

# ----------------------------------------------------
# [화면 1] 유저용 메인 화면
# ----------------------------------------------------
if menu == "유저: 나눔 기록하기":
    st.title("🐾 포켓몬 나눔 자동 로거")
    st.markdown("나눔 받은 게시글의 링크를 입력하여 아카이브에 영구 기록하세요. (내역은 관리자만 열람 가능합니다)")
    
    url_input = st.text_input("나눔 게시글 URL", placeholder="https://arca.live/b/pokemon/...")
    
    if st.button("내역 자동 추출하기", type="primary"):
        if not gemini_api_key:
            st.error("서버에 API 키가 설정되지 않아 기능을 사용할 수 없습니다.")
        elif url_input:
            with st.spinner("분석 중입니다..."):
                conn = get_db_connection()
                existing = conn.execute('SELECT * FROM logs WHERE url = ?', (url_input,)).fetchone()
                
                if existing and existing["status"] == "COMPLETED":
                    st.success("이미 분석이 완료되어 관리자 아카이브에 저장된 글입니다.")
                else:
                    raw_text = scrape_post(url_input)
                    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    
                    if "Error" in raw_text or "로그인" in raw_text or "성인" in raw_text:
                        try:
                            conn.execute(
                                'INSERT OR REPLACE INTO logs (url, status, created_at) VALUES (?, ?, ?)', 
                                (url_input, "FAILED_AUTH", current_time)
                            )
                            conn.commit()
                        except sqlite3.IntegrityError:
                            pass
                        st.warning("⚠️ 인증이 필요한 글이거나 차단되었습니다. 관리자 대기열에 등록되었습니다.")
                    else:
                        try:
                            extracted_data = extract_giveaway_data(raw_text, gemini_api_key)
                            conn.execute(
                                'INSERT OR REPLACE INTO logs (url, status, data, created_at) VALUES (?, ?, ?, ?)',
                                (url_input, "COMPLETED", extracted_data.model_dump_json(), current_time)
                            )
                            conn.commit()
                            st.success("✅ 나눔 내역이 관리자 아카이브에 성공적으로 저장되었습니다!")
                        except Exception as e:
                            st.error(f"분석 중 오류 발생: {e}")
                
                conn.close()

# ----------------------------------------------------
# [화면 2] 관리자 전용 화면 (Batch + 통계 + 초기화)
# ----------------------------------------------------
elif menu == "관리자: 일괄 처리 및 통계":
    st.title("🛠️ 관리자 대시보드")
    
    admin_pw = st.text_input("관리자 비밀번호", type="password")
    
    if admin_pw == "rabbit777": 
        # 관리자 화면을 탭으로 분리
        tab1, tab2 = st.tabs(["🚀 미처리 링크 일괄 뚫기", "📊 유저별 나눔 통계 및 데이터 관리"])
        
        # --- 탭 1: 일괄 뚫기 ---
        with tab1:
            conn = get_db_connection()
            pending_logs = conn.execute('SELECT url FROM logs WHERE status = "FAILED_AUTH"').fetchall()
            
            st.info(f"현재 인증 장벽에 막혀 대기 중인 링크: **{len(pending_logs)}개**")
            
            if pending_logs:
                for log in pending_logs:
                    st.code(log["url"])
                    
                # 🔑 수정됨: 입력창 삭제하고 숨겨진 쿠키(secret_arca_cookie)를 바로 사용합니다.
                if st.button("🔥 숨겨진 쿠키로 일괄 뚫기 실행", type="primary"):
                    if not secret_arca_cookie:
                        st.error("서버 Secrets에 ARCA_COOKIE가 설정되지 않았습니다. 셋팅을 확인해주세요.")
                    else:
                        progress_text = "일괄 크롤링 중입니다. 잠시만 대기해주세요..."
                        my_bar = st.progress(0, text=progress_text)
                        success_count = 0
                        total = len(pending_logs)
                        
                        for idx, log in enumerate(pending_logs):
                            target_url = log["url"]
                            my_bar.progress((idx + 1) / total, text=f"처리 중: {target_url}")
                            
                            raw_text = scrape_post(target_url, secret_arca_cookie)
                            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            
                            if "Error" not in raw_text and "로그인" not in raw_text:
                                try:
                                    extracted_data = extract_giveaway_data(raw_text, gemini_api_key)
                                    conn.execute(
                                        'UPDATE logs SET status = ?, data = ?, created_at = ? WHERE url = ?',
                                        ("COMPLETED", extracted_data.model_dump_json(), current_time, target_url)
                                    )
                                    conn.commit()
                                    success_count += 1
                                except Exception as e:
                                    st.write(f"LLM 추출 실패 ({target_url}): {e}")
                            else:
                                st.write(f"크롤링 재실패 (삭제된 글이거나 만료된 쿠키): {target_url}")
                                
                        my_bar.empty()
                        st.success(f"🎉 일괄 처리 완료! 총 {total}개 중 {success_count}개 뚫기 성공.")
                        time.sleep(2)
                        st.rerun() 
            conn.close()

        # --- 탭 2: 나눔 내역 통계 및 관리 ---
        with tab2:
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
                    
                date_key = dt.strftime("%Y-%m-%d") # 날짜 (예: 2026-08-17)
                week_key = dt.strftime("%Y-%W")    # 연도-주차 (예: 2026-34)
                weekday_name = weekdays_ko[dt.weekday()] # 요일
                
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
                # 규정 위반 체크 (하루 2회 초과 OR 주간 5회 초과)
                over_daily = any(count > 2 for count in stats["by_date"].values())
                over_weekly = any(count > 5 for count in stats["by_week"].values())
                
                if over_daily or over_weekly:
                    display_name = f":red[{uname} 🚨 (규정 초과)]"
                else:
                    display_name = f"{uname}"
                    
                with st.expander(f"👤 {display_name} - 총 {stats['total']}회 수령"):
                    # 요일별 수령 내역
                    weekday_texts = [f"{w} {stats['weekdays'][w]}회" for w in weekdays_ko if stats['weekdays'][w] > 0]
                    st.markdown(f"**[요일별 누적 수령]** {', '.join(weekday_texts)}")
                    
                    # 상세 기록
                    st.markdown("**[상세 내역]**")
                    for h in stats["history"]:
                        st.write(f"- {h}")
                        
            if not user_stats:
                st.info("아직 저장된 나눔 완료 내역이 없습니다.")
                
            # 3. 데이터 초기화 버튼
            st.markdown("---")
            st.subheader("🚨 데이터 초기화 구역")
            st.warning("이 버튼을 누르면 서버에 저장된 모든 나눔 기록 및 대기열 데이터가 영구적으로 삭제됩니다.")
            
            if st.button("모든 나눔 데이터 삭제", type="primary"):
                conn.execute('DELETE FROM logs')
                conn.commit()
                st.success("✅ 모든 데이터가 성공적으로 초기화되었습니다.")
                time.sleep(1.5)
                st.rerun()
                
            conn.close()
            
    elif admin_pw:
        st.error("비밀번호가 틀렸습니다.")
