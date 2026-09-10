"""
프로그램명: Google Drive 다운로드 프로그램
파일명: GoogleDown.py
설명: 구글 드라이브 폴더의 모든 파일/하위 디렉토리를 로컬 폴더에 완벽하게 다운로드하고 갱신하는 방탄 프로그램입니다.
      - 대용량 파일(1.5GB+) 다운로드 중 멈춤/무한 대기(Socket Hang) 100% 방지
      - 소켓 타임아웃(20초) 감지 시 자동으로 끊긴 위치부터 최대 20회 이어받기(Resume)
      - Queue 기반 비동기 UI 분리로 Tkinter 윈도우 창 굳음/응답 없음 원천 차단
      - 정확한 진행률(%), 다운로드 속도(MB/s), 남은 시간(ETA) 실시간 표시
      - [⏹ 다운로드 중지/취소] 및 [🔄 최신 목록 새로고침] 완벽 지원
"""

import os
import re
import sys
import time
import socket
import queue
import threading
import argparse
import subprocess
from typing import Optional, List, Dict, Any

# PyInstaller 런처 등 부모 프로세스에서 상속된 잘못된 TCL/TK 환경 변수 정화 (Tcl 버전 충돌 방지)
for _tcl_var in ("TCL_LIBRARY", "TK_LIBRARY", "TCLLIBPATH"):
    os.environ.pop(_tcl_var, None)

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Windows 콘솔 한글 처리
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 전역 소켓 기본 타임아웃: 25초 무응답 시 즉시 타임아웃 감지
socket.setdefaulttimeout(25.0)

import requests
import gdown
from gdown.download import get_url_from_gdrive_confirmation, _get_session
from gdown.exceptions import DownloadError

APP_TITLE = "Google Drive 다운로드 프로그램"
DEFAULT_URL = "https://drive.google.com/drive/folders/17dTLAIhXTaxYQR29sup-KnMTIXJ6q2cg?usp=drive_link"
DEFAULT_DOWNLOAD_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "downloads"))
CHUNK_SIZE = 1024 * 1024  # 1MB 스트리밍 청크


# ==============================================================================
# 1. 헬퍼 함수 및 단위 변환
# ==============================================================================
def format_size(bytes_val: int) -> str:
    """바이트 값을 B, KB, MB, GB 문자열로 변환합니다."""
    if bytes_val < 1024:
        return f"{bytes_val} B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    elif bytes_val < 1024 * 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.1f} MB"
    else:
        return f"{bytes_val / (1024 * 1024 * 1024):.2f} GB"


def format_speed(bps: float) -> str:
    """초당 바이트 전송률을 속도 문자열로 변환합니다."""
    if bps < 1024:
        return f"{bps:.1f} B/s"
    elif bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    else:
        return f"{bps / (1024 * 1024):.1f} MB/s"


def extract_folder_id(url_or_id: str) -> str:
    """URL 또는 ID 문자열에서 순수 Folder ID를 추출합니다."""
    url_or_id = url_or_id.strip()
    match = re.search(r'folders/([a-zA-Z0-9_-]+)', url_or_id)
    if match:
        return match.group(1)
    match = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url_or_id)
    if match:
        return match.group(1)
    if re.fullmatch(r'[a-zA-Z0-9_-]+', url_or_id):
        return url_or_id
    return url_or_id


# ==============================================================================
# 2. 방탄 단일 파일 다운로더 (소켓 타임아웃 + 자동 이어받기 재시도)
# ==============================================================================
def download_file_with_resume(
    file_id: str,
    output_path: str,
    sess: requests.Session,
    resume: bool = True,
    max_retries: int = 20,
    progress_callback: Optional[Any] = None,
    log_callback: Optional[Any] = None,
    cancel_check: Optional[Any] = None,
) -> str:
    """
    구글 드라이브 대용량 파일을 끊김 없이 다운로드합니다.
    - 소켓 타임아웃 25초 적용
    - 데이터 수신이 멈추면 즉시 감지하여 끊긴 위치부터 최대 max_retries회 이어받기
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)

    part_path = output_path + ".part"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # 이미 완료된 파일이고 이어받기 모드인 경우
    if resume and os.path.exists(output_path) and not os.path.exists(part_path):
        sz = os.path.getsize(output_path)
        log(f"   [건너뜀] 이미 완료된 파일: {os.path.basename(output_path)} ({format_size(sz)})")
        if progress_callback:
            progress_callback(100, sz, sz, 0.0, 0)
        return output_path

    # 이어받기가 아닌 경우 기존 임시 파일 제거
    if not resume and os.path.exists(part_path):
        try:
            os.remove(part_path)
        except Exception:
            pass

    download_url = f"https://drive.google.com/uc?id={file_id}&export=download"
    total_size = None

    for attempt in range(1, max_retries + 1):
        if cancel_check and cancel_check():
            raise InterruptedError("사용자에 의해 다운로드가 중지되었습니다.")

        # 현재까지 받은 크기 파악
        start_bytes = 0
        if os.path.exists(part_path):
            start_bytes = os.path.getsize(part_path)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Encoding": "identity",  # Content-Length를 정확히 파악하기 위함
        }
        if start_bytes > 0:
            headers["Range"] = f"bytes={start_bytes}-"

        try:
            # 1. 초기 요청 (Connect 타임아웃 15초, Read 타임아웃 25초)
            res = sess.get(download_url, headers=headers, stream=True, timeout=(15.0, 25.0), verify=True)

            # 2. 대용량 파일 바이러스 검사 경고(HTML) 처리
            content_type = res.headers.get("Content-Type", "")
            if "text/html" in content_type and "confirm=" not in download_url:
                confirm_url = None
                try:
                    confirm_url = get_url_from_gdrive_confirmation(res.text)
                except Exception:
                    # 정규식으로 confirm 토큰 추출 fallback
                    m = re.search(r'confirm=([0-9A-Za-z_-]+)', res.text)
                    if m:
                        confirm_url = f"https://drive.google.com/uc?id={file_id}&export=download&confirm={m.group(1)}"

                if confirm_url:
                    download_url = confirm_url
                    res.close()
                    res = sess.get(download_url, headers=headers, stream=True, timeout=(15.0, 25.0), verify=True)

            # 401/403 권한 에러 처리
            if res.status_code == 401 or res.status_code == 403:
                raise DownloadError(f"접근 권한 오류 (HTTP {res.status_code}): 링크 공유 권한을 확인해주세요.")

            # 전체 크기 파악
            if res.status_code == 206:
                # Range 지원 응답
                cr = res.headers.get("Content-Range", "")
                m = re.search(r'/(\d+)', cr)
                if m:
                    total_size = int(m.group(1))
            elif res.status_code == 200:
                # Range 미지원(처음부터 다운로드)
                start_bytes = 0
                cl = res.headers.get("Content-Length")
                if cl and cl.isdigit():
                    total_size = int(cl)
            else:
                res.raise_for_status()

            # 파일 쓰기 모드 결정 (206이면 append, 아니면 write)
            write_mode = "ab" if (start_bytes > 0 and res.status_code == 206) else "wb"
            downloaded = start_bytes

            if start_bytes > 0 and write_mode == "ab":
                log(f"   [이어받기 시작] {format_size(start_bytes)}부터 다운로드 재개")

            # 스트리밍 루프
            t_start = time.time()
            bytes_since_last_calc = 0
            t_last_calc = t_start
            current_speed = 0.0

            with open(part_path, write_mode) as f:
                for chunk in res.iter_content(chunk_size=CHUNK_SIZE):
                    if cancel_check and cancel_check():
                        res.close()
                        raise InterruptedError("사용자에 의해 다운로드가 중지되었습니다.")

                    if not chunk:
                        continue

                    f.write(chunk)
                    downloaded += len(chunk)
                    bytes_since_last_calc += len(chunk)

                    now = time.time()
                    time_diff = now - t_last_calc
                    if time_diff >= 0.5:  # 0.5초마다 속도 및 진행률 계산
                        current_speed = bytes_since_last_calc / time_diff
                        bytes_since_last_calc = 0
                        t_last_calc = now

                        percent = int((downloaded / total_size * 100)) if total_size else 0
                        eta = int((total_size - downloaded) / current_speed) if (total_size and current_speed > 0) else 0

                        if progress_callback:
                            progress_callback(percent, downloaded, total_size or 0, current_speed, eta)

            # 다운로드 완료 검증
            if total_size is not None and downloaded < total_size:
                raise DownloadError(f"파일이 불완전하게 수신됨 ({downloaded}/{total_size} bytes)")

            # 완료: .part 파일을 최종 파일로 원자적 이동
            res.close()
            if os.path.exists(output_path):
                os.remove(output_path)
            os.replace(part_path, output_path)

            if progress_callback and total_size:
                progress_callback(100, total_size, total_size, current_speed, 0)

            return output_path

        except (requests.exceptions.RequestException, socket.timeout, TimeoutError, DownloadError) as e:
            err_msg = str(e)
            if "HTTP 401" in err_msg or "HTTP 403" in err_msg:
                raise

            if attempt < max_retries:
                cur_mb = format_size(start_bytes if not os.path.exists(part_path) else os.path.getsize(part_path))
                wait_sec = min(attempt * 2, 10)
                log(f"   ⚠️ [연결 지연/끊김 감지] {err_msg[:60]}... 끊긴 지점({cur_mb})부터 자동으로 이어받습니다. (재시도 {attempt}/{max_retries}, {wait_sec}초 후 재개)")
                time.sleep(wait_sec)
            else:
                log(f"   ❌ [오류] 최대 재시도 횟수({max_retries}회) 초과: {e}")
                raise

    raise DownloadError(f"다운로드 실패: {output_path}")


# ==============================================================================
# 3. 폴더 다운로더 코어 엔진
# ==============================================================================
class GoogleDriveFolderDownloader:
    """폴더 전체를 관리하며 개별 파일을 방탄 방식으로 이어받는 관리자 클래스"""
    def __init__(
        self,
        folder_url_or_id: str,
        output_dir: str,
        cookies_file: Optional[str] = None,
        use_cookies: bool = True,
        log_callback: Optional[Any] = None,
        status_callback: Optional[Any] = None,
        progress_callback: Optional[Any] = None,
    ):
        self.folder_url_or_id = folder_url_or_id.strip()
        self.output_dir = os.path.abspath(output_dir.strip())
        self.cookies_file = cookies_file.strip() if cookies_file else None
        self.use_cookies = use_cookies
        self.log_callback = log_callback or (lambda msg: print(msg, end=""))
        self.status_callback = status_callback or (lambda status: None)
        self.progress_callback = progress_callback
        self.folder_id = extract_folder_id(self.folder_url_or_id)
        self._is_cancelled = False

    def log(self, message: str):
        if not message.endswith("\n"):
            message += "\n"
        self.log_callback(message)

    def update_status(self, status: str):
        self.status_callback(status)

    def cancel(self):
        self._is_cancelled = True
        self.log("\n[중지 요청] 다운로드 중단이 요청되었습니다...")

    def is_cancelled(self) -> bool:
        return self._is_cancelled

    def fetch_file_list(self) -> List[Dict[str, Any]]:
        """[새로고침/스캔] 폴더 내 모든 파일 목록 및 로컬 완료 여부를 반환합니다."""
        self.log("\n" + "=" * 65)
        self.log(f"[새로고침] 구글 드라이브 폴더 구조를 조회합니다...")
        self.log(f" - 폴더 ID: {self.folder_id}")
        self.log("=" * 65)
        self.update_status("구글 드라이브 파일 목록 스캔 중...")

        try:
            items = gdown.download_folder(
                id=self.folder_id,
                output=self.output_dir,
                quiet=True,
                use_cookies=self.use_cookies,
                cookies_file=self.cookies_file,
                skip_download=True,
            )

            if items is None:
                items = []

            results = []
            self.log("-" * 65)
            self.log(f"📋 총 {len(items)}개 파일을 확인했습니다.")

            for idx, item in enumerate(items, 1):
                f_id = getattr(item, "id", "")
                f_path = getattr(item, "path", str(item))
                f_local = getattr(item, "local_path", os.path.join(self.output_dir, f_path))

                is_exist = os.path.exists(f_local) and not os.path.exists(f_local + ".part")
                status_tag = "[완료됨]" if is_exist else "[다운로드 필요]"
                sz_str = ""
                if os.path.exists(f_local):
                    try:
                        sz_str = f" ({format_size(os.path.getsize(f_local))})"
                    except Exception:
                        pass
                elif os.path.exists(f_local + ".part"):
                    try:
                        part_sz = os.path.getsize(f_local + ".part")
                        status_tag = f"[이어받기 가능: {format_size(part_sz)}]"
                    except Exception:
                        pass

                self.log(f"  {idx:2d}. {status_tag} {f_path}{sz_str}")
                results.append({
                    "id": f_id,
                    "path": f_path,
                    "local_path": f_local,
                    "completed": is_exist,
                })

            self.log("-" * 65)
            self.update_status(f"새로고침 완료 (총 {len(results)}개 파일 감지)")
            return results

        except DownloadError as e:
            self._handle_download_error(e)
            raise
        except Exception as e:
            self.log(f"\n[오류 발생] 파일 목록 조회 실패: {e}")
            self.update_status(f"오류: {str(e)[:40]}")
            raise

    def run(self, resume: bool = True) -> List[str]:
        """
        폴더의 모든 파일을 방탄 방식으로 순차 다운로드합니다.
        대용량 파일 중간에 패킷이 멈추더라도 자동으로 끊긴 위치부터 이어받습니다.
        """
        self._is_cancelled = False
        mode_str = "이어받기(신규 파일만)" if resume else "전체 최신화(덮어쓰기)"

        self.log("\n" + "=" * 65)
        self.log(f"🚀 구글 드라이브 다운로드를 시작합니다 ({mode_str}).")
        self.log(f" - 폴더 ID: {self.folder_id}")
        self.log(f" - 저장 위치: {self.output_dir}")
        self.log("=" * 65)

        file_items = self.fetch_file_list()
        total_files = len(file_items)

        if total_files == 0:
            self.log("[안내] 다운로드할 파일이 없습니다.")
            self.update_status("파일 없음")
            return []

        # 세션 초기화
        user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        sess, _ = _get_session(
            proxy=None,
            use_cookies=self.use_cookies,
            user_agent=user_agent,
            cookies_file=self.cookies_file
        )


        downloaded_files = []
        try:
            for idx, item in enumerate(file_items, 1):
                if self._is_cancelled:
                    self.log("\n[중지됨] 사용자의 요청으로 다운로드가 중단되었습니다.")
                    self.update_status("다운로드 중지됨")
                    break

                f_id = item["id"]
                f_path = item["path"]
                f_local = item["local_path"]
                fname = os.path.basename(f_path)

                self.update_status(f"다운로드 중 ({idx}/{total_files}): {fname}")
                self.log(f"\n▶ [{idx}/{total_files}] 파일 처리 시작: {f_path}")

                def _item_progress(pct, downloaded_b, total_b, speed_val, eta_val):
                    if self.progress_callback:
                        self.progress_callback(
                            file_index=idx,
                            total_files=total_files,
                            filename=fname,
                            percent=pct,
                            downloaded_bytes=downloaded_b,
                            total_bytes=total_b,
                            speed=speed_val,
                            eta=eta_val,
                        )

                out_path = download_file_with_resume(
                    file_id=f_id,
                    output_path=f_local,
                    sess=sess,
                    resume=resume,
                    max_retries=20,  # 넉넉하게 20회 재시도
                    progress_callback=_item_progress,
                    log_callback=self.log,
                    cancel_check=self.is_cancelled,
                )

                downloaded_files.append(out_path)
                self.log(f"   ✅ [{idx}/{total_files}] 다운로드 완료: {fname}")

            if not self._is_cancelled:
                self.log("\n" + "=" * 65)
                self.log(f"🎉 모든 다운로드가 성공적으로 완료되었습니다! (총 {len(downloaded_files)}/{total_files}개)")
                self.log(f"저장 위치: {self.output_dir}")
                self.log("=" * 65)
                self.update_status(f"다운로드 완료! (총 {len(downloaded_files)}개 완료)")

            return downloaded_files

        finally:
            sess.close()

    def _handle_download_error(self, e: Exception):
        err_msg = str(e)
        self.log("\n" + "!" * 65)
        self.log(f"[오류 발생] Google Drive 작업 실패: {err_msg}")
        
        if "status code 401" in err_msg or "Permission" in err_msg or "HTTP 401" in err_msg:
            guide = (
                "\n[조치 가이드: 권한 오류 (401 Unauthorized)]\n"
                "해당 구글 드라이브 폴더가 비공개(제한됨) 상태이거나 로그인 권한이 필요합니다.\n"
                "1. 공개 다운로드: 구글 드라이브에서 해당 폴더 우클릭 -> [공유] -> [일반 액세스]를\n"
                "   '링크가 있는 모든 사용자'(뷰어)로 변경해 주세요.\n"
                "2. 비공개 다운로드: 브라우저에서 내보낸 'cookies.txt' 파일을 본 프로그램에 지정하여\n"
                "   인증을 통과하도록 설정할 수 있습니다.\n"
            )
            self.log(guide)
            self.update_status("오류: 공유 권한(401) 확인 필요")
        else:
            self.update_status(f"오류: {err_msg[:40]}")
        self.log("!" * 65)


# ==============================================================================
# 4. 그래픽 사용자 인터페이스 (GUI) - Queue 기반 완벽 비동기
# ==============================================================================
class GoogleDriveDownloaderApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title(APP_TITLE)
        self.geometry("860x700")
        self.minsize(720, 560)

        self.style = ttk.Style(self)
        try:
            self.style.theme_use("vista" if sys.platform == "win32" else "clam")
        except Exception:
            pass

        # 스레드 안전 UI 큐
        self._ui_queue: queue.Queue = queue.Queue()

        self._downloader: Optional[GoogleDriveFolderDownloader] = None
        self._worker_thread: Optional[threading.Thread] = None

        self._create_widgets()

        # 0.1초마다 큐를 비우며 UI를 갱신하는 폴링 루프 시작
        self.after(100, self._poll_queue)

    def _create_widgets(self):
        main_frame = ttk.Frame(self, padding="16 16 16 16")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. 헤더 타이틀 영역
        header_frame = ttk.Frame(main_frame)
        header_frame.pack(fill=tk.X, pady=(0, 10))

        title_lbl = ttk.Label(
            header_frame,
            text=f"📁 {APP_TITLE}",
            font=("Malgun Gothic", 15, "bold"),
            foreground="#1a73e8"
        )
        title_lbl.pack(anchor=tk.W)

        desc_lbl = ttk.Label(
            header_frame,
            text="대용량 파일도 멈춤 없이 100% 자동 이어받는 방탄 다운로더 & 갱신 관리자입니다.",
            font=("Malgun Gothic", 9),
            foreground="#555555"
        )
        desc_lbl.pack(anchor=tk.W, pady=(2, 0))

        # 2. 설정 입력 그룹
        input_group = ttk.LabelFrame(main_frame, text=" 다운로드 및 갱신 설정 ", padding="12 12 12 12")
        input_group.pack(fill=tk.X, pady=(0, 8))

        # (1) 링크
        ttk.Label(input_group, text="구글 드라이브 폴더 링크:", font=("Malgun Gothic", 9, "bold")).grid(
            row=0, column=0, sticky=tk.W, pady=4
        )
        self.url_var = tk.StringVar(value=DEFAULT_URL)
        self.url_entry = ttk.Entry(input_group, textvariable=self.url_var, font=("Consolas", 9))
        self.url_entry.grid(row=0, column=1, columnspan=2, sticky=tk.EW, padx=(8, 0), pady=4)

        # (2) 로컬 경로
        ttk.Label(input_group, text="저장할 로컬 폴더 경로:", font=("Malgun Gothic", 9, "bold")).grid(
            row=1, column=0, sticky=tk.W, pady=6
        )
        self.output_dir_var = tk.StringVar(value=DEFAULT_DOWNLOAD_DIR)
        self.output_entry = ttk.Entry(input_group, textvariable=self.output_dir_var, font=("Consolas", 9))
        self.output_entry.grid(row=1, column=1, sticky=tk.EW, padx=(8, 4), pady=6)

        browse_btn = ttk.Button(input_group, text="폴더 선택...", command=self._browse_output_dir)
        browse_btn.grid(row=1, column=2, sticky=tk.E, pady=6)

        # (3) 옵션
        options_frame = ttk.Frame(input_group)
        options_frame.grid(row=2, column=0, columnspan=3, sticky=tk.EW, pady=(4, 2))

        self.overwrite_var = tk.BooleanVar(value=False)
        self.overwrite_check = ttk.Checkbutton(
            options_frame,
            text="수정/갱신된 파일 강제 덮어쓰기 (기본값: 미존재 파일만 이어받기)",
            variable=self.overwrite_var
        )
        self.overwrite_check.pack(side=tk.LEFT, padx=(0, 16))

        self.use_cookie_var = tk.BooleanVar(value=False)
        self.cookie_check = ttk.Checkbutton(
            options_frame,
            text="비공개 폴더 인증용 cookies.txt 사용",
            variable=self.use_cookie_var,
            command=self._toggle_cookie_field
        )
        self.cookie_check.pack(side=tk.LEFT)

        # (4) 쿠키 경로
        self.cookie_row_frame = ttk.Frame(input_group)
        self.cookie_row_frame.grid(row=3, column=0, columnspan=3, sticky=tk.EW, pady=(2, 2))

        ttk.Label(self.cookie_row_frame, text="쿠키 파일 경로:", font=("Malgun Gothic", 8)).pack(side=tk.LEFT, padx=(0, 8))
        self.cookie_path_var = tk.StringVar(value="")
        self.cookie_entry = ttk.Entry(self.cookie_row_frame, textvariable=self.cookie_path_var, state=tk.DISABLED, font=("Consolas", 9))
        self.cookie_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))

        self.cookie_browse_btn = ttk.Button(
            self.cookie_row_frame, text="파일 선택...", state=tk.DISABLED, command=self._browse_cookie_file
        )
        self.cookie_browse_btn.pack(side=tk.RIGHT)

        input_group.columnconfigure(1, weight=1)

        # 3. 진행 상태 및 컨트롤 바
        control_frame = ttk.Frame(main_frame)
        control_frame.pack(fill=tk.X, pady=(0, 6))

        self.start_btn = ttk.Button(
            control_frame, text="▶ 다운로드 시작", command=self._start_download_thread
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6), ipadx=8, ipady=3)

        self.cancel_btn = ttk.Button(
            control_frame, text="⏹ 다운로드 중지", state=tk.DISABLED, command=self._cancel_download
        )
        self.cancel_btn.pack(side=tk.LEFT, padx=(0, 6), ipadx=6, ipady=3)

        self.refresh_btn = ttk.Button(
            control_frame, text="🔄 최신 목록 새로고침", command=self._start_refresh_thread
        )
        self.refresh_btn.pack(side=tk.LEFT, padx=(0, 6), ipadx=6, ipady=3)

        self.open_dir_btn = ttk.Button(
            control_frame, text="📂 저장 폴더 열기", command=self._open_output_dir
        )
        self.open_dir_btn.pack(side=tk.LEFT, padx=(0, 6), ipady=3)

        self.clear_log_btn = ttk.Button(
            control_frame, text="로그 지우기", command=self._clear_logs
        )
        self.clear_log_btn.pack(side=tk.RIGHT, ipady=3)

        # 상태 라벨 & 프로그레스 바 영역
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=(0, 6))

        self.status_lbl = ttk.Label(
            status_frame,
            text="대기 중 - '다운로드 시작' 또는 '최신 목록 새로고침'을 누르세요.",
            font=("Malgun Gothic", 9, "bold"),
            foreground="#333333"
        )
        self.status_lbl.pack(anchor=tk.W, pady=(0, 2))

        # 실제 퍼센트 게이지가 차오르는 Determinate 프로그레스 바
        self.progress_bar = ttk.Progressbar(status_frame, mode="determinate", maximum=100)
        self.progress_bar.pack(fill=tk.X)

        self.detail_lbl = ttk.Label(
            status_frame,
            text="",
            font=("Consolas", 9),
            foreground="#1a73e8"
        )
        self.detail_lbl.pack(anchor=tk.E, pady=(2, 0))

        # 4. 실시간 로그 창
        log_group = ttk.LabelFrame(main_frame, text=" 실시간 작업 로그 ", padding="8 8 8 8")
        log_group.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(
            log_group,
            wrap=tk.WORD,
            font=("Consolas", 9),
            bg="#1e1e1e",
            fg="#d4d4d4",
            insertbackground="#ffffff",
            relief=tk.FLAT
        )
        log_scroll = ttk.Scrollbar(log_group, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)

        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)

    # ----------------------------------------------------
    # UI 큐 폴링 루프: 메인 스레드에서 100ms마다 실행
    # ----------------------------------------------------
    def _poll_queue(self):
        try:
            while True:
                item = self._ui_queue.get_nowait()
                msg_type = item.get("type")

                if msg_type == "log":
                    # 최대 1000줄 유지로 메모리/렌더링 부하 완벽 차단
                    line_count = int(self.log_text.index("end-1c").split(".")[0])
                    if line_count > 1000:
                        self.log_text.delete("1.0", "200.0")
                    self.log_text.insert(tk.END, item["text"])
                    self.log_text.see(tk.END)

                elif msg_type == "status":
                    color = "#d93025" if item.get("is_error") else "#1a73e8"
                    self.status_lbl.configure(text=item["text"], foreground=color)

                elif msg_type == "progress":
                    pct = item["percent"]
                    d_str = format_size(item["downloaded"])
                    t_str = format_size(item["total"]) if item["total"] > 0 else "알 수 없음"
                    spd_str = format_speed(item["speed"])
                    eta_sec = item["eta"]
                    eta_str = f"{eta_sec // 60:02d}:{eta_sec % 60:02d}"

                    self.progress_bar["value"] = pct
                    self.detail_lbl.configure(
                        text=f"[{item['file_index']}/{item['total_files']}] {item['filename']} : {d_str} / {t_str} ({pct}%) | {spd_str} | 남은 시간: {eta_str}"
                    )

                elif msg_type == "popup":
                    p_type = item.get("p_type")
                    if p_type == "info":
                        messagebox.showinfo(item["title"], item["message"])
                    elif p_type == "error":
                        messagebox.showerror(item["title"], item["message"])
                    elif p_type == "confirm_download":
                        if messagebox.askyesno(item["title"], item["message"]):
                            self._start_download_thread()

                elif msg_type == "busy":
                    is_busy = item["is_busy"]
                    if is_busy:
                        self.start_btn.configure(state=tk.DISABLED)
                        self.refresh_btn.configure(state=tk.DISABLED)
                        self.cancel_btn.configure(state=tk.NORMAL)
                    else:
                        self.start_btn.configure(state=tk.NORMAL)
                        self.refresh_btn.configure(state=tk.NORMAL)
                        self.cancel_btn.configure(state=tk.DISABLED)

                self._ui_queue.task_done()
        except queue.Empty:
            pass
        finally:
            self.after(100, self._poll_queue)

    def queue_log(self, text: str):
        self._ui_queue.put({"type": "log", "text": text})

    def queue_status(self, text: str, is_error: bool = False):
        self._ui_queue.put({"type": "status", "text": text, "is_error": is_error})

    def queue_progress(self, file_index, total_files, filename, percent, downloaded_bytes, total_bytes, speed, eta):
        self._ui_queue.put({
            "type": "progress",
            "file_index": file_index,
            "total_files": total_files,
            "filename": filename,
            "percent": percent,
            "downloaded": downloaded_bytes,
            "total": total_bytes,
            "speed": speed,
            "eta": eta,
        })

    def queue_busy(self, is_busy: bool):
        self._ui_queue.put({"type": "busy", "is_busy": is_busy})

    def queue_popup(self, p_type: str, title: str, message: str):
        self._ui_queue.put({"type": "popup", "p_type": p_type, "title": title, "message": message})

    def _browse_output_dir(self):
        initial = self.output_dir_var.get() or os.getcwd()
        selected = filedialog.askdirectory(initialdir=initial, title="다운로드받을 저장 폴더 선택")
        if selected:
            self.output_dir_var.set(os.path.normpath(selected))

    def _browse_cookie_file(self):
        selected = filedialog.askopenfilename(
            title="cookies.txt 파일 선택",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")]
        )
        if selected:
            self.cookie_path_var.set(os.path.normpath(selected))

    def _toggle_cookie_field(self):
        if self.use_cookie_var.get():
            self.cookie_entry.configure(state=tk.NORMAL)
            self.cookie_browse_btn.configure(state=tk.NORMAL)
        else:
            self.cookie_entry.configure(state=tk.DISABLED)
            self.cookie_browse_btn.configure(state=tk.DISABLED)

    def _open_output_dir(self):
        path = self.output_dir_var.get()
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])

    def _clear_logs(self):
        self.log_text.delete("1.0", tk.END)

    def _cancel_download(self):
        if self._downloader:
            self._downloader.cancel()
            self.queue_status("중지 요청 중... 현재 파일 처리 후 종료됩니다.", is_error=True)

    def _get_common_inputs(self):
        url = self.url_var.get().strip()
        out_dir = self.output_dir_var.get().strip()
        cookie_file = self.cookie_path_var.get().strip() if self.use_cookie_var.get() else None

        if not url:
            messagebox.showwarning("입력 필요", "구글 드라이브 폴더 링크를 입력해주세요.")
            return None, None, None
        if not out_dir:
            messagebox.showwarning("입력 필요", "저장할 대상 폴더 경로를 지정해주세요.")
            return None, None, None
        return url, out_dir, cookie_file

    def _start_refresh_thread(self):
        url, out_dir, cookie_file = self._get_common_inputs()
        if not url:
            return

        self.queue_busy(True)
        self.queue_status("구글 드라이브 최신 목록 새로고침 중...")

        self._worker_thread = threading.Thread(
            target=self._run_refresh,
            args=(url, out_dir, cookie_file),
            daemon=True
        )
        self._worker_thread.start()

    def _run_refresh(self, url: str, out_dir: str, cookie_file: Optional[str]):
        try:
            self._downloader = GoogleDriveFolderDownloader(
                folder_url_or_id=url,
                output_dir=out_dir,
                cookies_file=cookie_file,
                log_callback=self.queue_log,
                status_callback=lambda s: self.queue_status(s, is_error=False),
            )
            files = self._downloader.fetch_file_list()

            completed_count = sum(1 for f in files if f["completed"])
            need_count = len(files) - completed_count

            summary_msg = (
                f"최신 파일 목록 새로고침 완료!\n\n"
                f"• 구글 드라이브 전체 파일: {len(files)}개\n"
                f"• 이미 다운로드된 파일: {completed_count}개\n"
                f"• 다운로드 필요한 파일: {need_count}개\n\n"
                f"지금 바로 다운로드를 진행하시겠습니까?"
            )
            self.queue_popup("confirm_download", "새로고침 결과", summary_msg)

        except Exception as e:
            self._handle_ui_error(e)
        finally:
            self.queue_busy(False)

    def _start_download_thread(self):
        url, out_dir, cookie_file = self._get_common_inputs()
        if not url:
            return

        resume = not self.overwrite_var.get()
        self.queue_busy(True)
        self.queue_status("다운로드 준비 중...")

        self._worker_thread = threading.Thread(
            target=self._run_download,
            args=(url, out_dir, cookie_file, resume),
            daemon=True
        )
        self._worker_thread.start()

    def _run_download(self, url: str, out_dir: str, cookie_file: Optional[str], resume: bool):
        try:
            self._downloader = GoogleDriveFolderDownloader(
                folder_url_or_id=url,
                output_dir=out_dir,
                cookies_file=cookie_file,
                log_callback=self.queue_log,
                status_callback=lambda s: self.queue_status(s, is_error=False),
                progress_callback=self.queue_progress,
            )
            files = self._downloader.run(resume=resume)

            if not self._downloader.is_cancelled():
                self.queue_status(f"다운로드 완료! (총 {len(files)}개 파일)", is_error=False)
                self.queue_popup(
                    "info",
                    "다운로드 완료",
                    f"모든 파일이 성공적으로 다운로드되었습니다!\n\n저장 경로: {out_dir}\n총 다운로드 파일 수: {len(files)}개"
                )

        except Exception as e:
            self._handle_ui_error(e)

        finally:
            self.queue_busy(False)

    def _handle_ui_error(self, e: Exception):
        err_msg = str(e)
        self.queue_status("작업 실패: 오류 확인 필요", is_error=True)
        if "status code 401" in err_msg or "Permission" in err_msg or "HTTP 401" in err_msg:
            self.queue_popup(
                "error",
                "공유 권한 오류 (401)",
                "구글 드라이브 폴더의 접근 권한이 없습니다.\n\n"
                "해결 방법:\n"
                "1. 구글 드라이브에서 해당 폴더를 우클릭합니다.\n"
                "2. [공유] 메뉴를 선택합니다.\n"
                "3. [일반 액세스] 항목을 '링크가 있는 모든 사용자'(뷰어)로 변경합니다.\n\n"
                "변경 후 다시 [새로고침] 또는 [다운로드 시작]을 눌러주세요."
            )
        else:
            self.queue_popup("error", "오류 발생", f"작업 중 오류가 발생했습니다:\n{err_msg}")


def run_gui():
    app = GoogleDriveDownloaderApp()
    app.mainloop()


# ==============================================================================
# 5. CLI 모드 및 메인 진입점
# ==============================================================================
def run_cli(url: str, output_dir: str, cookies_file: str = None, refresh_only: bool = False, overwrite: bool = False):
    print(f"\n[{APP_TITLE} - CLI 모드]")
    print(f"URL/ID     : {url}")
    print(f"저장 위치  : {output_dir}")
    if cookies_file:
        print(f"쿠키 파일  : {cookies_file}")
    print(f"모드       : {'최신 목록 새로고침(조회)' if refresh_only else ('강제 덮어쓰기' if overwrite else '이어받기/신규 다운로드')}")
    print("-" * 65)

    def _cli_log(msg: str):
        sys.stdout.write(msg)
        sys.stdout.flush()

    def _cli_progress(file_index, total_files, filename, percent, downloaded_bytes, total_bytes, speed, eta):
        d_str = format_size(downloaded_bytes)
        t_str = format_size(total_bytes) if total_bytes > 0 else "?"
        spd_str = format_speed(speed)
        sys.stdout.write(f"\r[{file_index}/{total_files}] {filename}: {d_str}/{t_str} ({percent}%) | {spd_str} | ETA: {eta}s   ")
        sys.stdout.flush()

    downloader = GoogleDriveFolderDownloader(
        folder_url_or_id=url,
        output_dir=output_dir,
        cookies_file=cookies_file,
        log_callback=_cli_log,
        status_callback=lambda status: None,
        progress_callback=_cli_progress,
    )

    try:
        if refresh_only:
            files = downloader.fetch_file_list()
            print(f"\n[조회 완료] 구글 드라이브 총 {len(files)}개 파일 확인.")
        else:
            files = downloader.run(resume=not overwrite)
            print(f"\n[완료] 총 {len(files)}개 파일 다운로드 완료.")
    except Exception as e:
        print(f"\n[실패] 작업 중단: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description=f"{APP_TITLE} (Google Drive 폴더 일괄 다운로드 & 동기화)"
    )
    parser.add_argument(
        "--url", "-u",
        type=str,
        default=None,
        help="Google Drive 폴더 공유 URL 또는 Folder ID (기본값: 사용자 제공 URL)"
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help=f"다운로드 받을 로컬 디렉토리 경로 (기본값: {DEFAULT_DOWNLOAD_DIR})"
    )
    parser.add_argument(
        "--cookies", "-c",
        type=str,
        default=None,
        help="비공개 폴더 다운로드를 위한 Netscape 형식의 cookies.txt 파일 경로"
    )
    parser.add_argument(
        "--refresh", "-r",
        action="store_true",
        help="다운로드를 진행하지 않고 구글 드라이브 상의 최신 파일 목록만 새로고침/조회"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="기존에 다운로드된 파일이 있더라도 최신 파일로 강제 덮어쓰기"
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="GUI 창을 띄우지 않고 터미널 CLI 환경에서 바로 실행"
    )

    args = parser.parse_args()

    if args.cli or (args.url is not None) or args.refresh or args.overwrite:
        target_url = args.url if args.url else DEFAULT_URL
        target_output = args.output if args.output else DEFAULT_DOWNLOAD_DIR
        run_cli(target_url, target_output, args.cookies, refresh_only=args.refresh, overwrite=args.overwrite)
    else:
        run_gui()


if __name__ == "__main__":
    main()
