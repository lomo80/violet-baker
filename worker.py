import os, sys, json, subprocess, shutil
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

# 1. 환경 설정
GITHUB_USER = "lomo80"
MASTER_FOLDER_ID = os.environ.get('MASTER_FOLDER_ID')
CHUNK_FOLDER_ID = os.environ.get('CHUNK_FOLDER_ID')

print(" [GitHub Worker] 드라이브 순찰을 시작합니다...")

# 2. 구글 드라이브 인증
creds_dict = json.loads(os.environ.get('GDRIVE_TOKEN'))
creds = Credentials.from_authorized_user_info(creds_dict)
drive = build('drive', 'v3', credentials=creds)

# 드라이브의 모든 파일을 '생성 시간 순'으로 줄 세워서 가져옵니다.
def get_all_files(folder_id):
    results = drive.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name, createdTime)",
        orderBy="createdTime"
    ).execute()
    return results.get('files', [])

chunk_files = get_all_files(CHUNK_FOLDER_ID)
master_files = get_all_files(MASTER_FOLDER_ID)

# 지시서(release_info_*)를 전부 찾아서 밀린 작업 목록(타임스탬프)을 만듭니다.
timestamps = []
for f in master_files:
    if f['name'].startswith('release_info_'):
        ts = f['name'].replace('release_info_', '').replace('.txt', '')
        timestamps.append(ts)

# 과거부터 최신순으로 정렬합니다. (앱 동기화 이빨 빠짐 방지!)
timestamps.sort()

if not timestamps:
    print(" 지시서가 없습니다. 드라이브가 깨끗하므로 퇴근합니다!")
    sys.exit(0)

print(f" [작업 발견] 총 {len(timestamps)}개의 밀린 작업을 순차 처리합니다: {timestamps}")

# Git 장부(sync-data) 세팅은 루프 돌기 전에 딱 한 번만 엽니다.
subprocess.run(['git', 'config', '--global', 'user.name', 'violet-baker-bot'])
subprocess.run(['git', 'config', '--global', 'user.email', 'bot@violet.local'])
gh_token = os.environ.get("GH_TOKEN")
if not os.path.exists('sync-data'):
    subprocess.run(['git', 'clone', f'https://x-access-token:{gh_token}@github.com/{GITHUB_USER}/sync-data.git'])

def download_file(file_id, file_name, dest_folder):
    request = drive.files().get_media(fileId=file_id)
    with io.FileIO(os.path.join(dest_folder, file_name), 'wb') as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    print(f"    다운로드 완료: {file_name}")


#  [완벽한 순차 처리] 시간표대로 하나씩 꺼내서 굽습니다.
for ts in timestamps:
    print(f"\n==================================================")
    print(f" [배송 시작] 타임스탬프: {ts} 작업 돌입")
    print(f"==================================================")

    # 이전 루프의 찌꺼기가 남지 않게 폴더를 싹 비우고 새로 엽니다.
    shutil.rmtree('dl_chunk', ignore_errors=True)
    shutil.rmtree('dl_master', ignore_errors=True)
    os.makedirs('dl_chunk', exist_ok=True)
    os.makedirs('dl_master', exist_ok=True)
    if os.path.exists('rawdata.7z'):
        os.remove('rawdata.7z')

    # 성공적으로 구워지면 드라이브에서 지울 파일들의 ID 바구니
    ids_to_delete = []

    # [1] CHUNK 파트 (해당 타임스탬프의 json, db만 핀셋으로 쏙 집어옵니다)
    c_db = next((f for f in chunk_files if f['name'] == f"data-{ts}.db"), None)
    c_json = next((f for f in chunk_files if f['name'] == f"data-{ts}.json"), None)

    if c_db and c_json:
        download_file(c_db['id'], c_db['name'], 'dl_chunk')
        download_file(c_json['id'], c_json['name'], 'dl_chunk')
        ids_to_delete.extend([c_db['id'], c_json['id']])
    else:
        print(f" [경고] {ts}의 청크 파일을 잃어버렸습니다! 이 시간표는 건너뜁니다.")
        continue

    # [2] MASTER 파트 (해당 타임스탬프의 db와 지시서)
    m_db = next((f for f in master_files if f['name'] == f"data-{ts}.db"), None)
    m_info = next((f for f in master_files if f['name'] == f"release_info_{ts}.txt"), None)

    if m_db and m_info:
        download_file(m_db['id'], m_db['name'], 'dl_master')
        download_file(m_info['id'], m_info['name'], 'dl_master')
        ids_to_delete.extend([m_db['id'], m_info['id']])

    # [3] MASTER JSON 11종 (가장 오래된 것부터 짝지어줍니다)
    json_names = ['index.json', 'tag-index.json', 'tag-artist.json', 'tag-group.json', 'tag-uploader.json', 'tag-series.json', 'tag-character.json', 'character-series.json', 'series-character.json', 'character-character.json', 'series-series.json']

    for j_name in json_names:
        j_file = next((f for f in master_files if f['name'] == j_name), None)
        if j_file:
            download_file(j_file['id'], j_file['name'], 'dl_master')
            ids_to_delete.append(j_file['id'])
            # 찾은 건 메모리에서 지워서 다음 루프 때 또 뽑히지 않게 막습니다.
            master_files.remove(j_file) 

    # 🚀 [수정] 압축하기 전에 dl_master 폴더에 있는 data-{ts}.db 파일을 data.db로 이름을 바꿉니다!
    os.rename(f"dl_master/data-{ts}.db", "dl_master/data.db")
    
    # [4] 7z 압축 (이제 안에는 항상 data.db라는 이름으로 들어갑니다!)
    print(f" {ts} 마스터 데이터 7z 압축 중...")
    subprocess.run(['7z', 'a', '-t7z', 'rawdata.7z', './dl_master/*'], check=True, stdout=subprocess.DEVNULL)
    
    # [5] GitHub 릴리즈
    print(f"☁️ {ts} GitHub 릴리즈 업로드 중...")
    db_size = os.path.getsize(f"dl_chunk/data-{ts}.db")
    json_size = os.path.getsize(f"dl_chunk/data-{ts}.json")

    subprocess.run([
        'gh', 'release', 'create', ts,
        f"dl_chunk/data-{ts}.db", f"dl_chunk/data-{ts}.json",
        '--repo', f"{GITHUB_USER}/chunk", '--title', f"chunk {ts}", '--notes', "Auto-chunk"
    ], check=True)

    subprocess.run([
        'gh', 'release', 'create', ts,
        'rawdata.7z',
        '--repo', f"{GITHUB_USER}/db", '--title', f"db {ts}", '--notes', "Auto-master"
    ], check=True)

    # [6] 장부 업데이트 & Commit/Push (루프 돌 때마다 안전하게 저장)
    print(f" {ts} 장부(sync-data) 업데이트 중...")
    lines = [
        f"chunk {ts} https://github.com/{GITHUB_USER}/chunk/releases/download/{ts}/data-{ts}.db {db_size}\n",
        f"chunk {ts} https://github.com/{GITHUB_USER}/chunk/releases/download/{ts}/data-{ts}.json {json_size}\n",
        f"db {ts} https://github.com/{GITHUB_USER}/db/releases/download/{ts}/rawdata.7z\n"
    ]
    with open('sync-data/syncversion.txt', 'a') as f:
        f.writelines(lines)

    subprocess.run(['git', '-C', 'sync-data', 'add', 'syncversion.txt'])
    subprocess.run(['git', '-C', 'sync-data', 'commit', '-m', f'sync: update {ts}'])
    subprocess.run(['git', '-C', 'sync-data', 'push'])

    # [7] 드라이브 삭제: 성공적으로 배송 끝난 "딱 그 파일들만" 핀셋 삭제!
    print(f" {ts} 처리 완료된 드라이브 파일만 안전하게 삭제 중...")
    for file_id in ids_to_delete:
        try: drive.files().delete(fileId=file_id).execute()
        except: pass

    print(f"✅ {ts} 사이클 완벽 종료!\n")

print(" 쌓여있던 밀린 드라이브 업무 모두 순차 처리 완료! 퇴근합니다!")
