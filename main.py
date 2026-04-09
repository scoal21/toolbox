from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List
import uvicorn
import sqlite3
import json
import os
import io
import time
import base64
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

app = FastAPI()

# ==========================================
# ⚙️ 구글 드라이브 설정 (Render 환경변수 활용)
# ==========================================
FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "1JW2F_eha2NfsUOWB0q9al7E__n20sMk_")
MAP_FILE_ID = os.environ.get("MAP_FILE_ID", "14BI4mQEE3RCgTksZ7XTYDbwraWr-L8-l") # 도면 파일 ID 추가
DB_FILENAME = "yard_tools.db"

def get_gdrive_service():
    creds_json = json.loads(os.environ.get("GOOGLE_CREDENTIALS"))
    creds = service_account.Credentials.from_service_account_info(creds_json)
    return build('drive', 'v3', credentials=creds)

# 1. DB 동기화 함수
def sync_db_from_drive():
    try:
        service = get_gdrive_service()
        query = f"name = '{DB_FILENAME}' and '{FOLDER_ID}' in parents and trashed = false"
        results = service.files().list(q=query).execute().get('files', [])
        
        if results:
            file_id = results[0]['id']
            request = service.files().get_media(fileId=file_id)
            fh = io.FileIO(DB_FILENAME, 'wb')
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            print("✅ 구글 드라이브에서 DB 다운로드 완료")
            return file_id
    except Exception as e:
        print(f"⚠️ DB 다운로드 실패 (초기 구동 시 무시 가능): {e}")
    return None

def upload_db_to_drive():
    try:
        service = get_gdrive_service()
        query = f"name = '{DB_FILENAME}' and '{FOLDER_ID}' in parents and trashed = false"
        results = service.files().list(q=query).execute().get('files', [])
        
        media = MediaFileUpload(DB_FILENAME, mimetype='application/octet-stream')
        if results:
            file_id = results[0]['id']
            service.files().update(fileId=file_id, media_body=media).execute()
        else:
            file_metadata = {'name': DB_FILENAME, 'parents': [FOLDER_ID]}
            service.files().create(body=file_metadata, media_body=media).execute()
    except Exception as e:
        print(f"⚠️ DB 업로드 실패: {e}")

# 2. 야드 도면 이미지 다운로드 함수 (신규)
def sync_map_from_drive():
    try:
        service = get_gdrive_service()
        request = service.files().get_media(fileId=MAP_FILE_ID)
        # index.html이 찾을 수 있도록 파일명을 yard_map.jpg로 강제 저장
        fh = io.FileIO("yard_map.jpg", 'wb')
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        print("✅ 구글 드라이브에서 야드 도면 다운로드 완료")
    except Exception as e:
        print(f"⚠️ 야드 도면 다운로드 실패: {e}")

# 앱 시작 시 구글 드라이브에서 DB와 도면을 로컬로 가져옴
sync_db_from_drive()
sync_map_from_drive()

# static 폴더 마운트를 파일 다운로드 이후로 배치하여 안전성 확보
app.mount("/static", StaticFiles(directory="."), name="static")

def init_db():
    conn = sqlite3.connect(DB_FILENAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS toolboxes 
                 (id TEXT PRIMARY KEY, name TEXT, lat REAL, lng REAL, color TEXT, 
                  manager_main TEXT, manager_sub TEXT, materials TEXT, warning TEXT, photos TEXT, is_locked INTEGER)''')
    conn.commit()
    conn.close()

init_db()
upload_db_to_drive() 

# --- 데이터 모델 ---
class Material(BaseModel):
    name: str; qty: str; note: str

class Toolbox(BaseModel):
    id: str; name: str; lat: float; lng: float; color: str; manager_main: str; manager_sub: str
    materials: List[Material]; warning: str = ""; photos: List[str] = []; is_locked: bool = False

# --- API 라우터 ---
@app.get("/", response_class=HTMLResponse)
async def get_webpage():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/toolboxes")
async def get_toolboxes():
    conn = sqlite3.connect(DB_FILENAME)
    c = conn.cursor()
    c.execute("SELECT * FROM toolboxes")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "lat": r[2], "lng": r[3], "color": r[4], 
             "manager_main": r[5], "manager_sub": r[6], "materials": json.loads(r[7]), 
             "warning": r[8], "photos": json.loads(r[9]), "is_locked": bool(r[10])} for r in rows]

@app.post("/api/toolboxes")
async def add_toolbox(box: Toolbox):
    conn = sqlite3.connect(DB_FILENAME)
    c = conn.cursor()
    c.execute("INSERT INTO toolboxes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
              (box.id, box.name, box.lat, box.lng, box.color, box.manager_main, box.manager_sub, 
               json.dumps([m.dict() for m in box.materials]), box.warning, json.dumps(box.photos), int(box.is_locked)))
    conn.commit()
    conn.close()
    upload_db_to_drive()
    return box

@app.put("/api/toolboxes/{box_id}")
async def update_toolbox(box_id: str, box: Toolbox):
    conn = sqlite3.connect(DB_FILENAME)
    c = conn.cursor()
    c.execute('''UPDATE toolboxes SET name=?, lat=?, lng=?, color=?, manager_main=?, manager_sub=?, materials=?, warning=?, photos=?, is_locked=? WHERE id=?''', 
              (box.name, box.lat, box.lng, box.color, box.manager_main, box.manager_sub, 
               json.dumps([m.dict() for m in box.materials]), box.warning, json.dumps(box.photos), int(box.is_locked), box_id))
    conn.commit()
    conn.close()
    upload_db_to_drive()
    return box

@app.delete("/api/toolboxes/{box_id}")
async def delete_toolbox(box_id: str):
    conn = sqlite3.connect(DB_FILENAME)
    c = conn.cursor()
    c.execute("DELETE FROM toolboxes WHERE id=?", (box_id,))
    conn.commit()
    conn.close()
    upload_db_to_drive()
    return {"message": "삭제 완료"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)