import os, sys, json, subprocess
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io


# 1. 환경 설정 

GITHUB_USER = "lomo80"
MASTER_FOLDER_ID = os.environ.get('MASTER_FOLDER_ID')
CHUNK_FOLDER_ID = os.environ.get('CHUNK_FOLDER_ID')

print(" [GitHub Worker] 드라이브 순찰을 시작합니다...")


# 2. 구글 드라이브 인증 및 '작업 지시서' 찾기

# GitHub Secrets에 등록해둔 GDRIVE_TOKEN을 꺼내서 인증서로 변환합니다.
creds_dict = json.loads(os.environ.get('GDRIVE_TOKEN'))
creds = Credentials.from_authorized_user_info(creds_dict)
drive = build('drive', 'v3', credentials=creds)

# 드라이브 마스터 폴더 안에 'release_info_' 로 시작하는 메모장이 있는지 검색합니다.
results = drive.files().list(q=f"'{MASTER_FOLDER_ID}' in parents and name contains 'release_info_' and trashed=false").execute()
info_files = results.get('files', [])

# 지시서가 없으면 아직 서버가 빵을 안 구웠다는 뜻이니 바로 잠듭니다.
if not info_files:
    print(" 지시서(release_info)가 없습니다. 드라이브에 새 파일이 없으므로 퇴근합니다!")
    sys.exit(0)

# 지시서가 있다면, 파일 이름에서 '시간표(Timestamp)' 숫자만 쏙 뽑아냅니다.
info_file = info_files[0]
timestamp = info_file['name'].replace('release_info_', '').replace('.txt', '')
print(f"[작업 발견] 타임스탬프: {timestamp} 포장 작업을 시작합니다!")


# 3. 구글 드라이브에서 파일 다운로드

# 파일을 다운로드할 임시 방(폴더)을 두 개 만듭니다. (청크와 마스터의 data.db 이름이 겹치기 때문)
os.makedirs('dl_chunk', exist_ok=True)
os.makedirs('dl_master', exist_ok=True)
downloaded_drive_ids = [info_file['id']] # 나중에 청소하기 위해 파일 ID를 기억해 둡니다.

def download_folder_files(folder_id, download_path):
    # 해당 폴더 안의 모든 파일을 가져옵니다.
    files = drive.files().list(q=f"'{folder_id}' in parents and trashed=false").execute().get('files', [])
    for f in files:
        print(f" 다운로드 중: {f['name']}")
        request = drive.files().get_media(fileId=f['id'])
        with io.FileIO(os.path.join(download_path, f['name']), 'wb') as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                _, done = downloader.next_chunk()
        downloaded_drive_ids.append(f['id']) # 지울 목록에 추가

print("\n [청크 파일 다운로드]")
download_folder_files(CHUNK_FOLDER_ID, 'dl_chunk')
print("\n [마스터 파일 다운로드]")
download_folder_files(MASTER_FOLDER_ID, 'dl_master')


# 4. 마스터 데이터 7z 압축 (GitHub의 무료 CPU를 100% 활용!)

print("\n 마스터 데이터 12종 7z 압축 시작... (이 작업은 수십 초 걸립니다)")
# 'rawdata' 라는 이름으로 dl_master 폴더 안의 모든 파일을 7z로 압축합니다.
subprocess.run(['7z', 'a', '-t7z', 'rawdata.7z', './dl_master/*'], check=True)


# 5. GitHub Releases (저장소)에 파일 배포하기

print("\n GitHub Releases 배포 시작...")
# 청크 파일들 용량(Byte)을 달아봅니다. (나중에 syncversion.txt에 적기 위함)
db_size = os.path.getsize(f"dl_chunk/data-{timestamp}.db")
json_size = os.path.getsize(f"dl_chunk/data-{timestamp}.json")

# GitHub CLI(gh)를 사용해 'chunk' 저장소에 청크 2개를 릴리즈(업로드) 합니다.
subprocess.run([
    'gh', 'release', 'create', timestamp, 
    f"dl_chunk/data-{timestamp}.db", f"dl_chunk/data-{timestamp}.json", 
    '--repo', f"{GITHUB_USER}/chunk", '--title', f"chunk {timestamp}", '--notes', "Auto-chunk"
], check=True)

# 'db' 저장소에 방금 압축한 마스터 'rawdata' 7z 파일을 릴리즈(업로드) 합니다.
subprocess.run([
    'gh', 'release', 'create', timestamp, 
    'rawdata.7z', 
    '--repo', f"{GITHUB_USER}/db", '--title', f"db {timestamp}", '--notes', "Auto-master"
], check=True)


# 6. 장부(syncversion.txt) 업데이트

print("\n 장부(sync-data) 업데이트 시작...")
# 봇이 스스로 깃헙에 로그인하고 커밋할 수 있게 신분증을 설정합니다.
subprocess.run(['git', 'config', '--global', 'user.name', 'violet-baker-bot'])
subprocess.run(['git', 'config', '--global', 'user.email', 'bot@violet.local'])

# sync-data 저장소를 다운로드(clone) 해옵니다.
gh_token = os.environ.get("GH_TOKEN")
subprocess.run(['git', 'clone', f'https://x-access-token:{gh_token}@github.com/{GITHUB_USER}/sync-data.git'])

# 텍스트 3줄을 만듭니다.
lines = [
    f"chunk {timestamp} https://github.com/{GITHUB_USER}/chunk/releases/download/{timestamp}/data-{timestamp}.db {db_size}\n",
    f"chunk {timestamp} https://github.com/{GITHUB_USER}/chunk/releases/download/{timestamp}/data-{timestamp}.json {json_size}\n",
    f"db {timestamp} https://github.com/{GITHUB_USER}/db/releases/download/{timestamp}/rawdata.7z\n"
]

# syncversion.txt 맨 아래에 3줄을 덧붙입니다(Append).
with open('sync-data/syncversion.txt', 'a') as f:
    f.writelines(lines)

# 변경된 장부를 저장(Commit)하고 다시 깃헙에 올립니다(Push).
subprocess.run(['git', '-C', 'sync-data', 'add', 'syncversion.txt'])
subprocess.run(['git', '-C', 'sync-data', 'commit', '-m', f'sync: update syncversion.txt {timestamp}'])
subprocess.run(['git', '-C', 'sync-data', 'push'])


# 7. 구글 드라이브 청소 (다음번 헛스윙 방지)

print("\n 구글 드라이브 청소 중...")
# 배송이 끝난 파일들을 드라이브 휴지통에 넣지 않고 영구 삭제하여 용량을 비웁니다.
for file_id in downloaded_drive_ids:
    try: drive.files().delete(fileId=file_id).execute()
    except: pass

print(f" [완료] {timestamp} 버전 모든 클라우드 배송 및 장부 정리 완벽 종료!")
