from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional
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
# ⚙️ 구글 드라이브 설정 (Render 환경변수)
# ==========================================
FOLDER_ID = os.environ.get("1JW2F_eha2NfsUOWB0q9al7E__n20sMk_")
MAP_FILE_ID = os.environ.get("14BI4mQEE3RCgTksZ7XTYDbwraWr-L8-l")
DB_FILENAME = "yard_tools.db"

def get_gdrive_service():
    creds_raw = os.environ.get("GOOGLE_CREDENTIALS")
    creds_json = json.loads(creds_raw)
    if "private_key" in creds_json:
        creds_json["private_key"] = creds_json["private_key"].replace("\\n", "\n")
    creds = service_account.Credentials.from_service_account_info(creds_json)
    return build('drive', 'v3', credentials=creds)

# 1. DB 동기화
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
            print("✅ DB 다운로드 완료")
    except Exception as e:
        print(f"⚠️ DB 다운로드 오류: {e}")

def upload_db_to_drive():
    try:
        service = get_gdrive_service()
        query = f"name = '{DB_FILENAME}' and '{FOLDER_ID}' in parents and trashed = false"
        results = service.files().list(q=query).execute().get('files', [])
        
        media = MediaFileUpload(DB_FILENAME, mimetype='application/octet-stream')
        if results:
            service.files().update(fileId=results[0]['id'], media_body=media).execute()
        else:
            file_metadata = {'name': DB_FILENAME, 'parents': [FOLDER_ID]}
            service.files().create(body=file_metadata, media_body=media).execute()
        print("✅ DB 업로드 완료")
    except Exception as e:
        print(f"⚠️ DB 업로드 오류: {e}")

# 2. 도면 동기화
def sync_map_from_drive():
    try:
        if not MAP_FILE_ID: return
        service = get_gdrive_service()
        request = service.files().get_media(fileId=MAP_FILE_ID)
        fh = io.FileIO("yard_map.jpg", 'wb')
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        print("✅ 야드 도면 다운로드 완료")
    except Exception as e:
        print(f"⚠️ 야드 도면 오류: {e}")

# 3. 사진 업로드 처리 (수동 구글 링크 생성)
def upload_photo_to_drive(filename, base64_data):
    try:
        if "," in base64_data:
            base64_data = base64_data.split(",")[1]
        image_bytes = base64.b64decode(base64_data)
        service = get_gdrive_service()
        file_metadata = {'name': filename, 'parents': [FOLDER_ID]}
        media = MediaFileUpload(io.BytesIO(image_bytes), mimetype='image/jpeg', resumable=True)
        file = service.files().create(body=file_metadata, media_body=media, fields='id').execute()
        return f"https://drive.google.com/uc?id={file.get('id')}"
    except Exception as e:
        print(f"⚠️ 사진 업로드 오류: {e}")
        return None

# 초기화 실행
sync_db_from_drive()
sync_map_from_drive()

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

# --- 데이터 모델 (422 에러 방지용으로 느슨하게 설정) ---
class Toolbox(BaseModel):
    id: str; name: str; lat: float; lng: float; color: str; manager_main: str; manager_sub: str
    materials: list = []; warning: str = ""; photos: list = []; is_locked: bool = False

# --- API 엔드포인트 ---
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
               json.dumps(box.materials), box.warning, json.dumps(box.photos), int(box.is_locked)))
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
               json.dumps(box.materials), box.warning, json.dumps(box.photos), int(box.is_locked), box_id))
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

# 🔥 누락되었던 핵심 사진 업로드 창구 (404 에러 해결)
@app.post("/api/toolboxes/{box_id}/photos")
async def upload_photo(box_id: str, payload: dict):
    image_data = payload.get("image_data")
    if not image_data:
        raise HTTPException(status_code=400, detail="사진 데이터 없음")
    
    filename = f"{box_id}_{int(time.time())}.jpg"
    photo_url = upload_photo_to_drive(filename, image_data)
    
    if photo_url:
        conn = sqlite3.connect(DB_FILENAME)
        c = conn.cursor()
        c.execute("SELECT photos FROM toolboxes WHERE id=?", (box_id,))
        row = c.fetchone()
        if row:
            photos = json.loads(row[0]) if row[0] else []
            photos.append(photo_url)
            c.execute("UPDATE toolboxes SET photos=? WHERE id=?", (json.dumps(photos), box_id))
            conn.commit()
        conn.close()
        upload_db_to_drive()
        return {"photo_url": photo_url}
    raise HTTPException(status_code=500, detail="드라이브 업로드 실패")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
