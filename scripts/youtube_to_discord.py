import os
import re
import json
import time
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Tuple, Set
from tenacity import retry, stop_after_attempt, wait_exponential

import requests
import isodate
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# 로깅 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 환경 변수
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')
YOUTUBE_MODE = os.getenv('YOUTUBE_MODE', 'channels').lower()
YOUTUBE_CHANNEL_ID = os.getenv('YOUTUBE_CHANNEL_ID')
YOUTUBE_PLAYLIST_ID = os.getenv('YOUTUBE_PLAYLIST_ID')
YOUTUBE_PLAYLIST_SORT = os.getenv('YOUTUBE_PLAYLIST_SORT', 'default').lower()
YOUTUBE_SEARCH_KEYWORD = os.getenv('YOUTUBE_SEARCH_KEYWORD')
INIT_MAX_RESULTS = int(os.getenv('YOUTUBE_INIT_MAX_RESULTS') or '50')
MAX_RESULTS = int(os.getenv('YOUTUBE_MAX_RESULTS') or '10')
INITIALIZE_MODE_YOUTUBE = os.getenv('INITIALIZE_MODE_YOUTUBE', 'false').lower() == 'true'
ADVANCED_FILTER_YOUTUBE = os.getenv('ADVANCED_FILTER_YOUTUBE', '')
DATE_FILTER_YOUTUBE = os.getenv('DATE_FILTER_YOUTUBE', '')
DISCORD_WEBHOOK_YOUTUBE = os.getenv('DISCORD_WEBHOOK_YOUTUBE')
DISCORD_WEBHOOK_YOUTUBE_DETAILVIEW = os.getenv('DISCORD_WEBHOOK_YOUTUBE_DETAILVIEW')
DISCORD_AVATAR_YOUTUBE = os.getenv('DISCORD_AVATAR_YOUTUBE', '').strip()
DISCORD_USERNAME_YOUTUBE = os.getenv('DISCORD_USERNAME_YOUTUBE', '').strip()
LANGUAGE_YOUTUBE = os.getenv('LANGUAGE_YOUTUBE', 'English')
YOUTUBE_DETAILVIEW = os.getenv('YOUTUBE_DETAILVIEW', 'false').lower() == 'true'

# DB 설정
DB_PATH = 'youtube_videos.db'

def check_env_variables() -> None:
    """환경 변수가 올바르게 설정되어 있는지 확인합니다."""
    try:
        base_required_vars = ['YOUTUBE_API_KEY', 'YOUTUBE_MODE', 'DISCORD_WEBHOOK_YOUTUBE']
        mode_specific_required_vars = {
            'channels': ['YOUTUBE_CHANNEL_ID'],
            'playlists': ['YOUTUBE_PLAYLIST_ID', 'YOUTUBE_PLAYLIST_SORT'],
            'search': ['YOUTUBE_SEARCH_KEYWORD']
        }
        
        mode = os.getenv('YOUTUBE_MODE', '').lower()
        if mode not in mode_specific_required_vars:
            raise ValueError("YOUTUBE_MODE는 'channels', 'playlists', 'search' 중 하나여야 합니다.")
        
        required_vars = base_required_vars + mode_specific_required_vars[mode]
        missing_vars = [var for var in required_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"다음 환경 변수가 설정되지 않았습니다: {', '.join(missing_vars)}")
        
        if mode == 'playlists':
            playlist_sort = os.getenv('YOUTUBE_PLAYLIST_SORT', '').lower()
            if playlist_sort not in ['default', 'reverse', 'date_newest', 'date_oldest']:
                raise ValueError("YOUTUBE_PLAYLIST_SORT는 'default', 'reverse', 'date_newest', 'date_oldest' 중 하나여야 합니다.")
        
        logging.info(f"환경 변수 검증 완료: {', '.join(required_vars)}")
    except Exception as e:
        logging.error(f"환경 변수 검증 중 오류 발생: {e}")
        raise

def init_db(reset: bool = False) -> None:
    """데이터베이스를 초기화하거나 기존 데이터베이스를 사용합니다."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            if reset:
                c.execute("DROP TABLE IF EXISTS videos")
                logging.info("기존 videos 테이블 삭제됨")
            
            c.execute('''CREATE TABLE IF NOT EXISTS videos
                         (published_at TEXT,
                          channel_title TEXT,
                          channel_id TEXT,
                          title TEXT,
                          video_id TEXT PRIMARY KEY,
                          video_url TEXT,
                          description TEXT,
                          category_id TEXT,
                          category_name TEXT,
                          duration TEXT,
                          thumbnail_url TEXT,
                          tags TEXT,
                          live_broadcast_content TEXT,
                          scheduled_start_time TEXT,
                          caption TEXT,
                          source TEXT)''')
            
            c.execute("PRAGMA integrity_check")
            integrity_result = c.fetchone()[0]
            if integrity_result != "ok":
                logging.error(f"데이터베이스 무결성 검사 실패: {integrity_result}")
                raise sqlite3.IntegrityError("데이터베이스 무결성 검사 실패")
            
            c.execute("SELECT COUNT(*) FROM videos")
            count = c.fetchone()[0]
            
            if reset or count == 0:
                logging.info("새로운 데이터베이스가 초기화되었습니다.")
            else:
                logging.info(f"기존 데이터베이스를 사용합니다. 현재 {count}개의 항목이 있습니다.")
    except sqlite3.Error as e:
        logging.error(f"데이터베이스 초기화 중 오류 발생: {e}")
        raise

def initialize_database_if_needed():
    try:
        if INITIALIZE_MODE_YOUTUBE:
            init_db(reset=True)
            logging.info("초기화 모드로 실행 중: 데이터베이스를 재설정하고 모든 비디오를 다시 가져옵니다.")
        else:
            init_db()
    except sqlite3.Error as e:
        logging.error(f"데이터베이스 초기화 중 오류 발생: {e}")
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=5), retry_error_callback=lambda retry_state: None)
def build_youtube_client():
    """유튜브 API 클라이언트를 빌드합니다."""
    try:
        return build('youtube', 'v3', developerKey=YOUTUBE_API_KEY)
    except HttpError as e:
        logging.error(f"유튜브 클라이언트 생성 중 오류 발생: {e}")
        raise YouTubeAPIError("유튜브 API 클라이언트 생성 중 오류 발생")
    except Exception as e:
        logging.error(f"유튜브 클라이언트 생성 중 알 수 없는 오류 발생: {e}")
        raise YouTubeAPIError("유튜브 API 클라이언트 생성 중 알 수 없는 오류 발생")

def fetch_playlist_info_if_needed(youtube):
    try:
        if YOUTUBE_MODE == 'playlists':
            return fetch_playlist_info(youtube, YOUTUBE_PLAYLIST_ID)
        return None
    except HttpError as e:
        logging.error(f"플레이리스트 정보를 가져오는 중 오류 발생: {e}")
        raise
    except Exception as e:
        logging.error(f"플레이리스트 정보를 가져오는 중 알 수 없는 오류 발생: {e}")
        raise

def get_video_details_dict(youtube, video_ids):
    try:
        video_details = fetch_video_details(youtube, video_ids)
        return {video['id']: video for video in video_details}
    except HttpError as e:
        logging.error(f"비디오 세부 정보를 가져오는 중 오류 발생: {e}")
        raise
    except Exception as e:
        logging.error(f"비디오 세부 정보를 가져오는 중 알 수 없는 오류 발생: {e}")
        raise

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=5), retry_error_callback=lambda retry_state: None)
def save_video(video_data: Dict[str, Any]) -> None:
    """비디오 데이터를 데이터베이스에 저장합니다."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute('''INSERT OR REPLACE INTO videos 
                         (published_at, channel_title, channel_id, title, video_id, video_url, description, 
                         category_id, category_name, duration, thumbnail_url, tags, live_broadcast_content, 
                         scheduled_start_time, caption, source) 
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', 
                      (video_data['published_at'], video_data['channel_title'], video_data['channel_id'], 
                       video_data['title'], video_data['video_id'], video_data['video_url'], 
                       video_data['description'], video_data['category_id'], video_data['category_name'], 
                       video_data['duration'], video_data['thumbnail_url'], video_data['tags'], 
                       video_data['live_broadcast_content'], video_data['scheduled_start_time'], 
                       video_data['caption'], video_data['source']))
            conn.commit()
            logging.info(f"새 비디오 저장됨: {video_data['video_id']}")
    except sqlite3.IntegrityError as e:
        logging.error(f"데이터베이스 무결성 오류 발생: {e}")
        raise DatabaseError("데이터베이스 무결성 오류 발생")
    except sqlite3.Error as e:
        logging.error(f"비디오 저장 중 오류 발생: {e}")
        raise DatabaseError("비디오 저장 중 오류 발생")

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=5), retry_error_callback=lambda retry_state: None)
def fetch_videos(youtube, mode: str, channel_id: str, playlist_id: str, search_keyword: str) -> List[Tuple[str, Dict[str, Any]]]:
    """유튜브에서 비디오 목록을 가져옵니다."""
    try:
        if mode == 'channels':
            return fetch_channel_videos(youtube, channel_id)
        elif mode == 'playlists':
            return fetch_playlist_videos(youtube, playlist_id)
        elif mode == 'search':
            return fetch_search_videos(youtube, search_keyword)
        else:
            raise ValueError("잘못된 모드입니다.")
    except HttpError as e:
        logging.error(f"비디오 목록을 가져오는 중 오류 발생: {e}")
        raise YouTubeAPIError("유튜브 API 호출 중 오류 발생")
    except Exception as e:
        logging.error(f"비디오 목록을 가져오는 중 알 수 없는 오류 발생: {e}")
        raise YouTubeAPIError("유튜브 API 호출 중 알 수 없는 오류 발생")

def fetch_channel_videos(youtube, channel_id: str) -> List[Tuple[str, Dict[str, Any]]]:
    video_items = []
    next_page_token = None
    max_results = INIT_MAX_RESULTS if INITIALIZE_MODE_YOUTUBE else MAX_RESULTS
    results_per_page = 50

    while len(video_items) < max_results:
        try:
            response = youtube.search().list(
                channelId=channel_id,
                order='date',
                type='video',
                part='snippet,id',
                maxResults=min(results_per_page, max_results - len(video_items)),
                pageToken=next_page_token
            ).execute()

            for item in response.get('items', []):
                video_items.append((item['id']['videoId'], item['snippet']))
            
            next_page_token = response.get('nextPageToken')
            if not next_page_token:
                break
        except Exception as e:
            logging.error(f"Error fetching videos: {e}")
            break

    # 날짜를 기준으로 최신순으로 정렬
    video_items.sort(key=lambda x: x[1]['publishedAt'], reverse=True)
    
    # 최신 max_results 개수만큼 반환
    return video_items[:max_results]

def fetch_playlist_videos(youtube, playlist_id: str) -> List[Tuple[str, Dict[str, Any]]]:
    playlist_items = []
    next_page_token = None
    max_results = INIT_MAX_RESULTS if INITIALIZE_MODE_YOUTUBE else MAX_RESULTS
    results_per_page = 50

    while len(playlist_items) < max_results:
        playlist_request = youtube.playlistItems().list(
            part="snippet",
            playlistId=playlist_id,
            maxResults=min(results_per_page, max_results - len(playlist_items)),
            pageToken=next_page_token
        )
        playlist_response = playlist_request.execute()
        
        playlist_items.extend(playlist_response['items'])
        
        next_page_token = playlist_response.get('nextPageToken')
        if not next_page_token:
            break

    playlist_items = sort_playlist_items(playlist_items)
    
    return [(item['snippet']['resourceId']['videoId'], item['snippet']) for item in playlist_items[:max_results]]

def sort_playlist_items(playlist_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if YOUTUBE_PLAYLIST_SORT == 'reverse':
        playlist_items.reverse()
    elif YOUTUBE_PLAYLIST_SORT == 'date_newest':
        playlist_items.sort(key=lambda x: x['snippet']['publishedAt'], reverse=True)
    elif YOUTUBE_PLAYLIST_SORT == 'date_oldest':
        playlist_items.sort(key=lambda x: x['snippet']['publishedAt'])
    else:
        # 기본값은 position으로 설정 (재생목록 작성자가 의도한 순서)
        playlist_items.sort(key=lambda x: x['snippet']['position'])
    
    return playlist_items

def fetch_search_videos(youtube, search_keyword: str) -> List[Tuple[str, Dict[str, Any]]]:
    video_items = []
    next_page_token = None
    max_results = INIT_MAX_RESULTS if INITIALIZE_MODE_YOUTUBE else MAX_RESULTS
    results_per_page = 50

    while len(video_items) < max_results:
        response = youtube.search().list(
            q=search_keyword,
            order='date',
            type='video',
            part='snippet,id',
            maxResults=min(results_per_page, max_results - len(video_items)),
            pageToken=next_page_token
        ).execute()

        video_items.extend([(item['id']['videoId'], item['snippet']) for item in response.get('items', [])])
        
        next_page_token = response.get('nextPageToken')
        if not next_page_token:
            break

    # 오래된 순서에서 최신 순서로 정렬 (publishedAt 기준 오름차순)
    video_items.sort(key=lambda x: x[1]['publishedAt'])
    return video_items[:max_results]

def get_channel_thumbnail(youtube, channel_id: str) -> str:
    """채널 썸네일을 가져옵니다."""
    try:
        response = youtube.channels().list(
            part="snippet",
            id=channel_id
        ).execute()
        return response['items'][0]['snippet']['thumbnails']['default']['url']
    except Exception as e:
        logging.error(f"채널 썸네일을 가져오는 데 실패했습니다: {e}")
        return ""

def create_embed_message(video: Dict[str, Any], youtube) -> Dict[str, Any]:
    """임베드 메시지를 생성합니다."""
    channel_thumbnail = get_channel_thumbnail(youtube, video['channel_id'])
    
    tags = video['tags'].split(',') if video['tags'] else []
    formatted_tags = ' '.join(f'`{tag.strip()}`' for tag in tags)
    
    play_text = "Play Video" if LANGUAGE_YOUTUBE == 'English' else "영상 재생"
    play_link = f"https://www.youtube.com/watch?v={video['video_id']}"
    embed_link = f"https://www.youtube.com/embed/{video['video_id']}"
    
    embed = {
        "title": video['title'],
        "description": video['description'][:4096],  # Discord 제한
        "url": video['video_url'],
        "color": 16711680,  # Red color
        "fields": [
            {
                "name": "🆔 Video ID" if LANGUAGE_YOUTUBE == 'English' else "🆔 영상 ID",
                "value": f"`{video['video_id']}`"
            },            
            {
                "name": "📁 Category" if LANGUAGE_YOUTUBE == 'English' else "📁 영상 분류",
                "value": video['category_name']
            },
            {
                "name": "🏷️ Tags" if LANGUAGE_YOUTUBE == 'English' else "🏷️ 영상 태그",
                "value": formatted_tags if formatted_tags else "N/A"
            },
            {
                "name": "⌛ Duration" if LANGUAGE_YOUTUBE == 'English' else "⌛ 영상 길이",
                "value": video['duration']
            },            
            {
                "name": "🔡 Subtitle" if LANGUAGE_YOUTUBE == 'English' else "🔡 영상 자막",
                "value": f"[Download](https://downsub.com/?url={video['video_url']})"
            },
            {
                "name": "▶️ " + play_text,
                "value": f"[Embed]({embed_link})"
            }
        ],
        "author": {
            "name": video['channel_title'],
            "url": f"https://www.youtube.com/channel/{video['channel_id']}",
            "icon_url": channel_thumbnail
        },
        "footer": {
            "text": "YouTube",
            "icon_url": "https://icon.dataimpact.ing/media/original/youtube/youtube_social_circle_red.png"
        },
        "timestamp": video['published_at'],
        "image": {
            "url": video['thumbnail_url']
        }
    }
    
    return {
        "content": None,
        "embeds": [embed],
        "attachments": []
    }

@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=5), retry_error_callback=lambda retry_state: None)
def post_to_discord(message: str, is_embed: bool = False, is_detail: bool = False) -> None:
    """Discord에 메시지를 게시합니다."""
    headers = {'Content-Type': 'application/json'}
    
    if is_embed:
        payload = message
    else:
        payload = {"content": message}
        if DISCORD_AVATAR_YOUTUBE:
            payload["avatar_url"] = DISCORD_AVATAR_YOUTUBE
        if DISCORD_USERNAME_YOUTUBE:
            payload["username"] = DISCORD_USERNAME_YOUTUBE
    
    webhook_url = DISCORD_WEBHOOK_YOUTUBE_DETAILVIEW if is_detail and DISCORD_WEBHOOK_YOUTUBE_DETAILVIEW else DISCORD_WEBHOOK_YOUTUBE
    
    try:
        response = requests.post(webhook_url, json=payload, headers=headers)
        response.raise_for_status()
        logging.info(f"Discord에 메시지 게시 완료 ({'상세' if is_detail else '기본'} 웹훅)")
    except requests.RequestException as e:
        logging.error(f"Discord에 메시지를 게시하는 데 실패했습니다: {e}")
        raise DiscordWebhookError("Discord 웹훅 호출 중 오류 발생")
    time.sleep(2)  # 속도 제한

def parse_duration(duration: str) -> str:
    """영상 길이를 파싱합니다."""
    parsed_duration = isodate.parse_duration(duration)
    total_seconds = int(parsed_duration.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if LANGUAGE_YOUTUBE == 'Korean':
        if hours > 0:
            return f"{hours}시간 {minutes}분 {seconds}초"
        elif minutes > 0:
            return f"{minutes}분 {seconds}초"
        else:
            return f"{seconds}초"
    else:
        if hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"

def convert_to_local_time(published_at: str) -> str:
    """게시 시간을 현지 시간으로 변환합니다."""
    utc_time = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ")
    utc_time = utc_time.replace(tzinfo=timezone.utc)
    
    if LANGUAGE_YOUTUBE == 'Korean':
        # KST는 UTC+9
        kst_time = utc_time + timedelta(hours=9)
        return kst_time.strftime("%Y년 %m월 %d일 %H시 %M분")
    else:
        local_time = utc_time.astimezone()
        return local_time.strftime("%Y-%m-%d %H:%M:%S")        

def apply_advanced_filter(title: str, advanced_filter: str) -> bool:
    """고급 필터를 적용하여 제목을 필터링합니다."""
    if not advanced_filter:
        return True

    text_to_check = title.lower()
    terms = re.findall(r'([+-]?)(?:"([^"]*)"|\S+)', advanced_filter)

    for prefix, term in terms:
        term = term.lower() if term else prefix.lower()
        if prefix == '+' or not prefix:  # 포함해야 하는 단어
            if term not in text_to_check:
                return False
        elif prefix == '-':  # 제외해야 하는 단어 또는 구문
            exclude_terms = term.split()
            if len(exclude_terms) > 1:
                if ' '.join(exclude_terms) in text_to_check:
                    return False
            else:
                if term in text_to_check:
                    return False

    return True

def parse_date_filter(filter_string: str) -> Tuple[datetime, datetime, datetime]:
    """날짜 필터를 파싱합니다."""
    since_date = None
    until_date = None
    past_date = None

    logging.info(f"파싱 중인 날짜 필터 문자열: {filter_string}")

    if not filter_string:
        logging.warning("날짜 필터 문자열이 비어있습니다.")
        return since_date, until_date, past_date

    since_match = re.search(r'since:(\d{4}-\d{2}-\d{2})', filter_string)
    until_match = re.search(r'until:(\d{4}-\d{2}-\d{2})', filter_string)
    
    if since_match:
        since_date = datetime.strptime(since_match.group(1), '%Y-%m-%d').replace(tzinfo=timezone.utc)
        logging.info(f"since_date 파싱 결과: {since_date}")
    if until_match:
        until_date = datetime.strptime(until_match.group(1), '%Y-%m-%d').replace(tzinfo=timezone.utc)
        logging.info(f"until_date 파싱 결과: {until_date}")

    past_match = re.search(r'past:(\d+)([hdmy])', filter_string)
    if past_match:
        value = int(past_match.group(1))
        unit = past_match.group(2)
        now = datetime.now(timezone.utc)
        if unit == 'h':
            past_date = now - timedelta(hours=value)
        elif unit == 'd':
            past_date = now - timedelta(days=value)
        elif unit == 'm':
            past_date = now - timedelta(days=value*30)  # 근사값 사용
        elif unit == 'y':
            past_date = now - timedelta(days=value*365)  # 근사값 사용
        logging.info(f"past_date 파싱 결과: {past_date}")
    else:
        logging.warning("past: 형식의 날짜 필터를 찾을 수 없습니다.")

    logging.info(f"최종 파싱 결과 - since_date: {since_date}, until_date: {until_date}, past_date: {past_date}")
    return since_date, until_date, past_date

def is_within_date_range(published_at: str, since_date: datetime, until_date: datetime, past_date: datetime) -> bool:
    """게시물이 날짜 필터 범위 내에 있는지 확인합니다."""
    pub_datetime = datetime.strptime(published_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    
    if past_date and pub_datetime >= past_date:
        return True
    if since_date and pub_datetime >= since_date:
        return True
    if until_date and pub_datetime <= until_date:
        return True
    
    return False

# 카테고리 ID를 이름으로 변환하는 캐시를 이용한 함수
category_cache = {}
def get_category_name(youtube, category_id: str) -> str:
    """카테고리 ID를 카테고리 이름으로 변환합니다."""
    if category_id in category_cache:
        return category_cache[category_id]
    
    try:
        categories = youtube.videoCategories().list(part="snippet", regionCode="US").execute()
        for category in categories['items']:
            category_cache[category['id']] = category['snippet']['title']
            if category['id'] == category_id:
                return category['snippet']['title']
        return "Unknown"
    except Exception as e:
        logging.error(f"카테고리 이름을 가져오는 데 실패했습니다: {e}")
        return "Unknown"

def process_video(video: Dict[str, Any], youtube, playlist_info: Dict[str, str] = None) -> None:
    formatted_published_at = convert_to_local_time(video['published_at'])
    video_url = f"https://youtu.be/{video['video_id']}"
    
    message = create_discord_message(video, formatted_published_at, video_url, playlist_info)
    
    post_to_discord(message)
    
    if YOUTUBE_DETAILVIEW:
        post_detailed_view(video, youtube)
    
    try:
        save_video(video)
    except Exception as e:
        logging.error(f"비디오 저장 중 오류 발생: {e}")
    logging.info(f"비디오 정보 저장 완료: {video['title']}")

def create_discord_message(video: Dict[str, Any], formatted_published_at: str, video_url: str, playlist_info: Dict[str, str] = None) -> str:
    if LANGUAGE_YOUTUBE == 'Korean':
        return create_korean_message(video, formatted_published_at, video_url, playlist_info)
    else:
        return create_english_message(video, formatted_published_at, video_url, playlist_info)

def create_korean_message(video: Dict[str, Any], formatted_published_at: str, video_url: str, playlist_info: Dict[str, str] = None) -> str:
    source_text = get_source_text_korean(video, playlist_info)
    
    message = (
        f"{source_text}"
        f"**{video['title']}**\n"
        f"{video_url}\n\n"
        f"📁 카테고리: `{video['category_name']}`\n"
        f"⌛️ 영상 길이: `{video['duration']}`\n"
        f"📅 게시일: `{formatted_published_at}`\n"
        f"🖼️ [썸네일](<{video['thumbnail_url']}>)"
    )
    
    if video['scheduled_start_time']:
        formatted_start_time = convert_to_local_time(video['scheduled_start_time'])
        message += f"\n\n🔴 예정된 라이브 시작 시간: `{formatted_start_time}`"
    
    return message

def create_english_message(video: Dict[str, Any], formatted_published_at: str, video_url: str, playlist_info: Dict[str, str] = None) -> str:
    source_text = get_source_text_english(video, playlist_info)
    
    message = (
        f"{source_text}"
        f"**{video['title']}**\n"
        f"{video_url}\n\n"
        f"📁 Category: `{video['category_name']}`\n"
        f"⌛️ Duration: `{video['duration']}`\n"
        f"📅 Published: `{formatted_published_at}`\n"
        f"🖼️ [Thumbnail](<{video['thumbnail_url']}>)"
    )
    
    if video['scheduled_start_time']:
        formatted_start_time = convert_to_local_time(video['scheduled_start_time'])
        message += f"\n\n🔴 Scheduled Live Start Time: `{formatted_start_time}`"
    
    return message

def get_source_text_korean(video: Dict[str, Any], playlist_info: Dict[str, str] = None) -> str:
    if YOUTUBE_MODE == 'channels':
        return f"`{video['channel_title']} - YouTube`\n"
    elif YOUTUBE_MODE == 'playlists':
        if playlist_info:
            return f"`📃 {playlist_info['title']} - YouTube 재생목록 by {playlist_info['channel_title']}`\n\n`{video['channel_title']} - YouTube`\n"
        else:
            return f"`📃 YouTube 재생목록`\n`{video['channel_title']} - YouTube`\n"
    elif YOUTUBE_MODE == 'search':
        return f"`🔎 {YOUTUBE_SEARCH_KEYWORD} - YouTube 검색 결과`\n\n`{video['channel_title']} - YouTube`\n\n"
    else:
        logging.warning(f"알 수 없는 YOUTUBE_MODE: {YOUTUBE_MODE}")
        return f"`{video['channel_title']} - YouTube`\n"

def get_source_text_english(video: Dict[str, Any], playlist_info: Dict[str, str] = None) -> str:
    if YOUTUBE_MODE == 'channels':
        return f"`{video['channel_title']} - YouTube Channel`\n"
    elif YOUTUBE_MODE == 'playlists':
        if playlist_info:
            return f"`📃 {playlist_info['title']} - YouTube Playlist by {playlist_info['channel_title']}`\n\n`{video['channel_title']} - YouTube`\n"
        else:
            return f"`📃 YouTube Playlist`\n`{video['channel_title']} - YouTube`\n"
    elif YOUTUBE_MODE == 'search':
        return f"`🔎 {YOUTUBE_SEARCH_KEYWORD} - YouTube Search Result`\n\n`{video['channel_title']} - YouTube`\n\n"
    else:
        logging.warning(f"Unknown YOUTUBE_MODE: {YOUTUBE_MODE}")
        return f"`{video['channel_title']} - YouTube`\n"

def fetch_playlist_info(youtube, playlist_id: str) -> Dict[str, str]:
    try:
        playlist_response = youtube.playlists().list(
            part="snippet",
            id=playlist_id
        ).execute()
        
        if 'items' in playlist_response and playlist_response['items']:
            playlist_info = playlist_response['items'][0]['snippet']
            return {
                'title': playlist_info['title'],
                'channel_title': playlist_info['channelTitle']
            }
    except Exception as e:
        logging.error(f"재생목록 정보를 가져오는 데 실패했습니다: {e}")
    
    return None

def post_detailed_view(video: Dict[str, Any], youtube) -> None:
    logging.info(f"YOUTUBE_DETAILVIEW가 True입니다. 임베드 메시지 생성 및 전송 시도")
    try:
        embed_message = create_embed_message(video, youtube)
        logging.info(f"임베드 메시지 생성 완료: {video['title']}")
        time.sleep(1)  # Discord 웹훅 속도 제한 방지를 위한 대기
        post_to_discord(embed_message, is_embed=True, is_detail=True)
        logging.info(f"임베드 메시지 전송 완료: {video['title']}")
    except Exception as e:
        logging.error(f"임베드 메시지 생성 또는 전송 중 오류 발생: {e}")

def fetch_video_details(youtube, video_ids: List[str]) -> List[Dict[str, Any]]:
    """비디오 세부 정보를 가져옵니다."""
    video_details = []
    chunk_size = 50
    for i in range(0, len(video_ids), chunk_size):
        chunk = video_ids[i:i+chunk_size]
        try:
            video_details_response = youtube.videos().list(
                part="snippet,contentDetails,liveStreamingDetails",
                id=','.join(chunk)
            ).execute()
            video_details.extend(video_details_response.get('items', []))
        except Exception as e:
            logging.error(f"비디오 세부 정보를 가져오는 중 오류 발생: {e}")
    return video_details

def get_existing_video_ids() -> Set[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT video_id FROM videos")
    existing_video_ids = set(row[0] for row in c.fetchall())
    conn.close()
    return existing_video_ids

def process_new_videos(youtube, videos: List[Tuple[str, Dict[str, Any]]], video_details_dict: Dict[str, Dict[str, Any]], 
                       existing_video_ids: Set[str], since_date: datetime, until_date: datetime, past_date: datetime) -> List[Dict[str, Any]]:
    new_videos = []
    for video_id, snippet in videos:
        if video_id not in video_details_dict:
            logging.warning(f"비디오 세부 정보를 찾을 수 없음: {video_id}")
            continue

        video_detail = video_details_dict[video_id]
        snippet = video_detail['snippet']
        content_details = video_detail['contentDetails']
        live_streaming_details = video_detail.get('liveStreamingDetails', {})

        published_at = snippet['publishedAt']
        
        if video_id in existing_video_ids:
            logging.info(f"이미 존재하는 비디오 건너뛰기: {video_id}")
            continue

        # 초기 실행 시 날짜 필터 무시
        if not INITIALIZE_MODE_YOUTUBE and not is_within_date_range(published_at, since_date, until_date, past_date):
            logging.info(f"날짜 필터에 의해 건너뛰어진 비디오: {snippet['title']}")
            continue

        video_title = snippet['title']
        
        if not apply_advanced_filter(video_title, ADVANCED_FILTER_YOUTUBE):
            logging.info(f"고급 필터에 의해 건너뛰어진 비디오: {video_title}")
            continue

        new_videos.append(create_video_data(youtube, video_id, snippet, content_details, live_streaming_details))
    
    # 날짜순으로 정렬 (오래된 순)
    new_videos.sort(key=lambda x: x['published_at'])
    
    return new_videos

def create_video_data(youtube, video_id: str, snippet: Dict[str, Any], content_details: Dict[str, Any], live_streaming_details: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'published_at': snippet['publishedAt'],
        'channel_title': snippet['channelTitle'],
        'channel_id': snippet['channelId'],
        'title': snippet['title'],
        'video_id': video_id,
        'video_url': f"https://youtu.be/{video_id}",
        'description': snippet.get('description', ''),
        'category_id': snippet.get('categoryId', 'Unknown'),
        'category_name': get_category_name(youtube, snippet.get('categoryId', 'Unknown')),
        'duration': parse_duration(content_details['duration']),
        'thumbnail_url': snippet['thumbnails']['high']['url'],
        'tags': ','.join(snippet.get('tags', [])),
        'live_broadcast_content': snippet.get('liveBroadcastContent', ''),
        'scheduled_start_time': live_streaming_details.get('scheduledStartTime', ''),
        'caption': content_details.get('caption', ''),
        'source': YOUTUBE_MODE
    }

def log_execution_info():
    logging.info(f"YOUTUBE_MODE: {YOUTUBE_MODE}")
    logging.info(f"INITIALIZE_MODE_YOUTUBE: {INITIALIZE_MODE_YOUTUBE}")
    logging.info(f"YOUTUBE_DETAILVIEW: {YOUTUBE_DETAILVIEW}")
    logging.info(f"데이터베이스 파일 크기: {os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else '파일 없음'}")
    
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM videos")
        count = c.fetchone()[0]
        logging.info(f"데이터베이스의 비디오 수: {count}")

def main():
    try:
        check_env_variables()
        initialize_database_if_needed()
        youtube = build_youtube_client()
        playlist_info = fetch_playlist_info_if_needed(youtube)
        videos = fetch_videos(youtube, YOUTUBE_MODE, YOUTUBE_CHANNEL_ID, YOUTUBE_PLAYLIST_ID, YOUTUBE_SEARCH_KEYWORD)
        video_ids = [video[0] for video in videos]
        video_details_dict = get_video_details_dict(youtube, video_ids)
        existing_video_ids = get_existing_video_ids()
        
        # 기본값으로 날짜 필터 설정
        since_date, until_date, past_date = parse_date_filter(DATE_FILTER_YOUTUBE) if DATE_FILTER_YOUTUBE else (None, None, None)
        
        new_videos = process_new_videos(youtube, videos, video_details_dict, existing_video_ids, since_date, until_date, past_date)
        
        # 채널 모드와 검색 모드일 때는 오래된 순서부터 처리
        if YOUTUBE_MODE in ['channels', 'search']:
            new_videos = new_videos[::-1]  # 리스트를 뒤집어 최신 순으로 변경
        
        for video in new_videos:
            process_video(video, youtube, playlist_info)

        logging.info(f"새로운 비디오 수: {len(new_videos)}")
        log_execution_info()
        
    except YouTubeAPIError as e:
        logging.error(f"유튜브 API 오류 발생: {e}")
    except DatabaseError as e:
        logging.error(f"데이터베이스 오류 발생: {e}")
    except DiscordWebhookError as e:
        logging.error(f"디스코드 웹훅 오류 발생: {e}")
    except Exception as e:
        logging.error(f"알 수 없는 오류 발생: {e}", exc_info=True)
        sys.exit(1)
    finally:
        logging.info("스크립트 실행 완료")

if __name__ == "__main__":
    main()