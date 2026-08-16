import streamlit as st
import os
import time
import sqlite3
import json
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
# SQLite 데이터베이스 연결 (파일 형태로 서버에 저장됨)
def get_db_connection():
    conn = sqlite3.connect('giveaway_logs.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    # url: 게시글 주소 / status: 상태(PENDING, COMPLETED, FAILED_AUTH) / data: LLM 추출 결과 JSON
    conn.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            status TEXT,
            data TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db() # 앱 실행 시 DB가 없으면 생성

# --- 3. 핵심 기능 함수 (크롤링 및 추출) ---
def scrape_post(url: str, user_cookie: str = None) -> str:
    with sync_playwright() as p:
        # 봇 탐지 방지 플래그(args) 추가
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
        
        if user_cookie:
            context.add_cookies([
                {"name": "arca_session", "value": user_cookie.strip(), "domain": ".arca.live", "path": "/"}
            ])
            
        page = context.new_page()
        # webdriver 감지 비활성화 스크립트 주입
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

# Streamlit 서버의 Secrets에서 Gemini API 키 불러오기
try:
    gemini_api_key = st.secrets["GEMINI_API_KEY"]
except KeyError:
    gemini_api_key = None

# 사이드바 설정 및 메뉴 분리
with st.sidebar:
    st.header("🔑 메뉴")
    menu = st.radio("이동", ["유저: 나눔 기록하기", "관리자: 일괄 처리(Batch)"])
    
    if not gemini_api_key:
        st.error("⚠️ 시스템 오류: 서버(Secrets)에 Gemini API 키가 설정되지 않았습니다.")

# ----------------------------------------------------
# [화면 1] 유저용 메인 화면
# ----------------------------------------------------
if menu == "유저: 나눔 기록하기":
    st.title("🐾 포켓몬 나눔 자동 로거")
    url_input = st.text_input("나눔 게시글 URL", placeholder="https://arca.live/b/pokemon/...")
    
    if st.button("내역 자동 추출하기", type="primary"):
        if not gemini_api_key:
            st.error("서버에 API 키가 설정되지 않아 기능을 사용할 수 없습니다.")
        elif url_input:
            with st.spinner("분석 중입니다..."):
                conn = get_db_connection()
                
                # 이미 DB에 있는지 확인
                existing = conn.execute('SELECT * FROM logs WHERE url = ?', (url_input,)).fetchone()
                
                if existing and existing["status"] == "COMPLETED":
                    st.success("이미 분석이 완료되어 저장된 글입니다.")
                else:
                    # 쿠키 없이 크롤링 시도
                    raw_text = scrape_post(url_input)
                    
                    if "Error" in raw_text or "로그인" in raw_text or "성인" in raw_text:
                        # 막혔다면 DB에 FAILED_AUTH 상태로 대기열에 넣음
                        try:
                            conn.execute('INSERT OR REPLACE INTO logs (url, status) VALUES (?, ?)', (url_input, "FAILED_AUTH"))
                            conn.commit()
                        except sqlite3.IntegrityError:
                            pass
                        st.warning("⚠️ 인증이 필요한 글이거나 차단되었습니다. 관리자 대기열에 등록되었습니다. (나중에 자동 처리됩니다)")
                    else:
                        # 성공 시 데이터 추출 및 DB 저장
                        try:
                            extracted_data = extract_giveaway_data(raw_text, gemini_api_key)
                            conn.execute(
                                'INSERT OR REPLACE INTO logs (url, status, data) VALUES (?, ?, ?)',
                                (url_input, "COMPLETED", extracted_data.model_dump_json())
                            )
                            conn.commit()
                            st.success("✅ 나눔 내역이 성공적으로 영구 저장되었습니다!")
                        except Exception as e:
                            st.error(f"분석 중 오류 발생: {e}")
                
                conn.close()

    # DB에 저장된 완료 내역 보여주기
    st.markdown("---")
    st.subheader("📚 최근 아카이브된 나눔 내역")
    conn = get_db_connection()
    completed_logs = conn.execute('SELECT * FROM logs WHERE status = "COMPLETED" ORDER BY id DESC LIMIT 5').fetchall()
    
    for log in completed_logs:
        data = json.loads(log["data"])
        with st.expander(f"👑 주최자 {data['host_username']} 님의 나눔 (게시글: {log['url']})"):
            for recipient in data['recipients']:
                pkmn_names = ", ".join([p["name"] for p in recipient["received_pokemon"]])
                st.write(f"- 당첨자: **{recipient['username']}** 님 ➡️ 수령: {pkmn_names}")
    conn.close()

# ----------------------------------------------------
# [화면 2] 관리자 전용 화면 (Batch Processing)
# ----------------------------------------------------
elif menu == "관리자: 일괄 처리(Batch)":
    st.title("🛠️ 관리자 대기열 일괄 처리")
    
    admin_pw = st.text_input("관리자 비밀번호", type="password")
    
    if admin_pw == "rabbit777": 
        conn = get_db_connection()
        pending_logs = conn.execute('SELECT url FROM logs WHERE status = "FAILED_AUTH"').fetchall()
        
        st.info(f"현재 인증 장벽에 막혀 대기 중인 링크: **{len(pending_logs)}개**")
        
        if pending_logs:
            for log in pending_logs:
                st.code(log["url"])
                
            admin_cookie = st.text_input("본인의 arca_session 쿠키 값을 입력하세요", type="password")
            
            if st.button("🔥 쿠키 장전 및 일괄 뚫기 실행", type="primary"):
                if not admin_cookie:
                    st.error("쿠키를 입력해야 합니다.")
                elif not gemini_api_key:
                    st.error("서버에 API 키가 설정되지 않았습니다.")
                else:
                    progress_text = "일괄 크롤링 중입니다. 잠시만 대기해주세요..."
                    my_bar = st.progress(0, text=progress_text)
                    
                    success_count = 0
                    total = len(pending_logs)
                    
                    for idx, log in enumerate(pending_logs):
                        target_url = log["url"]
                        my_bar.progress((idx + 1) / total, text=f"처리 중: {target_url}")
                        
                        # 이번엔 관리자 쿠키를 넣고 강력하게 스크래핑 시도
                        raw_text = scrape_post(target_url, admin_cookie)
                        
                        if "Error" not in raw_text and "로그인" not in raw_text:
                            try:
                                extracted_data = extract_giveaway_data(raw_text, gemini_api_key)
                                conn.execute(
                                    'UPDATE logs SET status = ?, data = ? WHERE url = ?',
                                    ("COMPLETED", extracted_data.model_dump_json(), target_url)
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
                    st.rerun() # 화면 새로고침
        conn.close()
    elif admin_pw:
        st.error("비밀번호가 틀렸습니다.")
