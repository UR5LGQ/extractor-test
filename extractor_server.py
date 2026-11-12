import os
import re
import subprocess
import shutil
import queue
import threading
import time
import logging
import socket
from flask import Flask, Response, jsonify, request, render_template_string

# Отключаем рутинные логи Flask в консоли (оставляем только критические ошибки)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

# Очереди задач браузера и управления
browser_task_queue = queue.Queue()
browser_action_queue = queue.Queue()

# Состояние сервера
xvfb_proc = None
pw = None
browser = None
context = None
page = None
last_screenshot_bytes = b""
ffmpeg_proc = None
ffmpeg_lock = threading.Lock()
mode_lock = threading.Lock()
current_mode = "mag"                 # mag | login
display_ready_mode = "starting"      # framebuffer/viewport реально переключены
viewer_last_seen = 0.0               # heartbeat вкладки управления
last_video_id = "Z4szpuAFUco&pp=ygUK0YHQstCw0YLRiw%3D%3D"
FIFO_PATH = "/tmp/mag322_stream.fifo"

MAG_SIZE = {"width": 640, "height": 360}
LOGIN_SIZE = {"width": 854, "height": 480}  # 480p


def get_mode():
    with mode_lock:
        return current_mode


def set_mode_value(value):
    global current_mode
    with mode_lock:
        current_mode = value


def viewer_is_active():
    # Если вкладка закрылась или перестала отправлять heartbeat, считаем её неактивной.
    return (time.time() - viewer_last_seen) < 12.0



def browser_worker_loop():
    """Один постоянный Chrome; переключаем разрешение и страницу без перезапуска профиля."""
    global xvfb_proc, pw, browser, context, page, last_screenshot_bytes, last_video_id, display_ready_mode
    if not shutil.which("ffmpeg") or not shutil.which("Xvfb"):
        raise RuntimeError("FFmpeg or Xvfb is missing on server")

    display_str = ":99"
    lock_file = "/tmp/.X99-lock"
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
        except Exception:
            pass


    # Xvfb всегда работает в максимальном нужном режиме 854x480.
    # Менять framebuffer через xrandr не нужно: Xvfb выдавал ошибку,
    # если пытаться уменьшить его ниже активного выхода 1280x720.
    # Размер страницы Chrome переключается отдельно через Playwright viewport.
    xvfb_proc = subprocess.Popen(["Xvfb", display_str, "-screen", "0", "854x480x24"])
    # Запускаем оконный менеджер, который заставит любое окно встать в full-screen
    # subprocess.Popen(["matchbox-window-manager", "-use_titlebar", "no"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ["DISPLAY"] = display_str
    time.sleep(1)

    from playwright.sync_api import sync_playwright
    import json

    pw = sync_playwright().start()
    user_data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chrome_profile")
    context = pw.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        headless=False,
        # !!! ВАЖНО: Удаляем параметр viewport полностью (ставим None)
        viewport=None, 
        locale="ru-RU",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        args=[
            "--no-sandbox", 
            "--disable-dev-shm-usage", 
            "--autoplay-policy=no-user-gesture-required",
            "--start-fullscreen",
            "--kiosk",
            
            # 1. Убираем плашку "Chrome is being controlled..."
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
            
            # 2. Жестко задаем размер окна под размер экрана Xvfb
            "--window-size=854,480",
            "--window-position=0,0",
            
            # 3. Принудительный запуск в режиме веб-приложения (скрывает весь UI)
            f"--app=http://127.0.0.1:6901/embed?id={last_video_id}",
            "--alsa-output-device=hw:Loopback,0",
            
            # --- НОВЫЕ ФЛАГИ ДЛЯ СЛАБЫХ СЕРВЕРОВ (ЭКОНОМИЯ RAM/CPU) ---
            "--disable-gpu",                         # Отключаем GPU-ускорение (в Xvfb оно только жрёт ресурсы)
            "--disable-software-rasterizer",
            "--js-flags=--max-old-space-size=256",   # Ограничиваем память под JavaScript движком V8 до 256MB
            "--disable-background-networking",
            "--disable-component-update",
            # === РАДИКАЛЬНАЯ ОЧИСТКА RAM ===
            "--js-flags=--max-old-space-size=128",   # Жестко урезаем JS движок до 128 МБ
            "--no-zshman",
            "--renderer-process-limit=1",            # Запрещаем плодить процессы рендеринга
            "--disable-ndk-translation",
            "--lite-video-mode",                     # Включаем режим экономии трафика/памяти для видео
            "--aggressive-cache-discard",            # Chrome будет мгновенно стирать кеш из памяти
        ],
        # Отключаем внутренние флаги автоматизации Playwright, которые возвращают плашку
        ignore_default_args=["--enable-automation"]
    )


    cookies_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.json")
    if os.path.exists(cookies_path) and len(context.cookies()) == 0:
        try:
            with open(cookies_path, "r", encoding="utf-8") as f:
                context.add_cookies(json.load(f))
            print("[extractor] Successfully imported cookies.json", flush=True)
        except Exception as e:
            print(f"[extractor] Failed to load cookies: {e}", flush=True)

    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.set_viewport_size(MAG_SIZE)
    except Exception:
        pass
    initial_url = f"http://127.0.0.1:6901/embed?id={last_video_id}"
    print(f"[extractor] Opening local embed player: {initial_url}", flush=True)
    try:
        page.goto(initial_url, wait_until="domcontentloaded", timeout=15000)
    except Exception as e:
        print(f"[extractor] Startup load error: {e}", flush=True)
    display_ready_mode = "mag"

    last_shot_time = 0.0
    print("[extractor] Browser loop started in MAG322 mode", flush=True)

    while True:
        # Смена режима: Chrome и профиль остаются теми же.
        try:
            task = browser_task_queue.get_nowait()
            if isinstance(task, tuple) and task[0] == "mode":
                target_mode = task[1]
                size = LOGIN_SIZE if target_mode == "login" else MAG_SIZE
                try:
                    active_page = context.pages[0] if context.pages else page
                    active_page.set_viewport_size(size)
                    display_ready_mode = target_mode
                    if target_mode == "login":
                        active_page.goto("https://www.youtube.com", wait_until="domcontentloaded", timeout=20000)
                    else:
                        active_page.goto(f"http://127.0.0.1:6901/embed?id={last_video_id}", wait_until="domcontentloaded", timeout=20000)
                    print(f"[extractor] Switched browser to {target_mode}: {size['width']}x{size['height']}", flush=True)
                except Exception as e:
                    print(f"[extractor] Mode switch error: {e}", flush=True)
            else:
                video_id = task
                last_video_id = video_id
                if get_mode() == "mag":
                    try:
                        active_page = context.pages[0] if context.pages else page
                        active_page.goto(f"http://127.0.0.1:6901/embed?id={video_id}", wait_until="domcontentloaded", timeout=15000)
                    except Exception as e:
                        print(f"[extractor] Failed to switch video: {e}", flush=True)
            browser_task_queue.task_done()
        except queue.Empty:
            pass

        # Управление браузером разрешено только в режиме авторизации.
        if get_mode() == "login":
            try:
                action = browser_action_queue.get_nowait()
                action_type, *args = action
                active_page = context.pages[0] if context.pages else page
                if action_type == "click":
                    active_page.mouse.click(float(args[0]), float(args[1]))
                elif action_type == "move":
                    active_page.mouse.move(float(args[0]), float(args[1]))
                elif action_type == "type":
                    value = args[0]
                    key_map = {
                        "[ENTER]": "Enter", "[TAB]": "Tab", "[ESC]": "Escape",
                        "[BACKSPACE]": "Backspace", "[SPACE]": "Space",
                        "[UP]": "ArrowUp", "[DOWN]": "ArrowDown", "[LEFT]": "ArrowLeft",
                        "[RIGHT]": "ArrowRight", "[HOME]": "Home", "[END]": "End",
                        "[PAGEUP]": "PageUp", "[PAGEDOWN]": "PageDown", "[DELETE]": "Delete"
                    }
                    if value in key_map:
                        active_page.keyboard.press(key_map[value])
                    elif value.startswith("[KEY:") and value.endswith("]"):
                        key = value[5:-1]
                        allowed = {"Enter", "Tab", "Escape", "Backspace", "Delete", "Space", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"}
                        if key in allowed or (len(key) == 1 and key.isalnum()):
                            active_page.keyboard.press(key)
                    else:
                        active_page.keyboard.type(value, delay=0)
                browser_action_queue.task_done()
            except queue.Empty:
                pass
            except Exception as e:
                print(f"[browser] Action error: {e}", flush=True)
                try:
                    browser_action_queue.task_done()
                except Exception:
                    pass
        else:
            # Удаляем накопившиеся действия авторизации при возврате в MAG322.
            while True:
                try:
                    browser_action_queue.get_nowait()
                    browser_action_queue.task_done()
                except queue.Empty:
                    break

        # Скриншоты создаются только пока вкладка управления шлёт heartbeat.
        mode = get_mode()
        interval = 0.2 if mode == "mag" else 0.1
        now = time.time()
        if viewer_is_active() and now - last_shot_time >= interval:
            try:
                active_page = context.pages[0] if context.pages else page
                last_screenshot_bytes = active_page.screenshot(type="jpeg", quality=35 if mode == "mag" else 50)
                last_shot_time = now
            except Exception:
                pass
        elif not viewer_is_active():
            last_screenshot_bytes = b""
            last_shot_time = now

        time.sleep(0.01)

@app.route("/embed", methods=["GET"])
def embed_page():
    video_id = request.args.get("id", "Z4szpuAFUco&pp=ygUK0YHQstCw0YLRiw%3D%3D").strip()
    html_template = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Embed Player</title>
        <style>
            body, html {{ margin: 0; padding: 0; width: 100%; height: 100%; background-color: #000; overflow: hidden; }}
            iframe {{ width: 100%; height: 100%; border: none; }}
        </style>
    </head>
    <body>
        <iframe src="https://youtube.com/embed/{video_id}?autoplay=1&controls=0&disablekb=1&modestbranding=1&rel=0" 
                allow="autoplay; encrypted-media" allowfullscreen></iframe>
    </body>
    </html>
    """
    return render_template_string(html_template)

@app.route("/screenshot", methods=["GET"])
def get_screenshot():
    global last_screenshot_bytes
    if not last_screenshot_bytes:
        # Если поток еще не успел сделать первый снимок, отдаем пустой ответ или прозрачный пиксель
        return Response(b"", mimetype="image/jpeg")
        
    return Response(
        last_screenshot_bytes, 
        mimetype="image/jpeg", 
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    )

@app.route("/get_stream", methods=["GET"])
def get_stream():
    video_id = request.args.get("id", "").strip()
    if not VIDEO_ID_RE.fullmatch(video_id):
        return jsonify({"ok": False, "error": "Invalid video ID"}), 400
    
    if get_mode() != "mag":
        return jsonify({"ok": False, "error": "Сначала вернитесь в режим MAG322"}), 409
    browser_task_queue.put(video_id)
    stream_url = f"{request.host_url.rstrip('/')}/stream"
    return jsonify({
        "ok": True,
        "mode": "screen_cast_360p",
        "streamUrl": stream_url,
        "proxyUrl": stream_url
    })

def ffmpeg_worker_loop():
    """FFmpeg работает только в режиме MAG322; в режиме авторизации процесс завершается."""
    global ffmpeg_proc
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("[ffmpeg_worker] Critical: FFmpeg is missing!", flush=True)
        return

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "warning",
        
        # --- Жесткая глобальная синхронизация ---
        "-fflags", "+nobuffer+genpts+discardcorrupt",
        "-flags", "+low_delay",
        
        # --- Настройки захвата Видео ---
        "-f", "x11grab",
        "-video_size", "640x360", # Размер области плеера
        "-framerate", "25",                 # Ровно 25 кадров на входе
        "-i", ":99",
        
        # --- Захват звука Chromium через ALSA Loopback ---
        "-thread_queue_size", "512",
        "-f", "alsa",
        "-ac", "2",
        "-ar", "48000",
        "-i", "hw:Loopback,1",
       
        
        # --- ФИЛЬТРЫ: Принудительно режем FPS до 25 и фиксируем тайминги ---
        "-vf", "scale=640:360,fps=fps=25", 
        
        # --- Синхронизация потоков на выходе ---
        "-vsync", "1",                      # Принудительно дублировать/дропать кадры, сохраняя идеальный таймлайн
        
        # --- Кодирование Видео MPEG-2 ---
        "-c:v", "mpeg2video",
        "-b:v", "800k",
        "-maxrate", "900k",
        "-bufsize", "400k",
        "-g", "12",
        
        # --- Кодирование звука ---
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:a", "mp2",
        "-b:a", "128k",
        "-ar", "48000",
        "-ac", "2", 
        
        # --- Выходной поток ---
        "-f", "mpegts", 
        "udp://127.0.0.1:9999?pkt_size=1316&buffer_size=1048576&fifo_size=50000"
    ]



    while True:
        if get_mode() == "mag" and display_ready_mode == "mag":
            with ffmpeg_lock:
                running = ffmpeg_proc is not None and ffmpeg_proc.poll() is None
                if not running:
                    try:
                        ffmpeg_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        print("[ffmpeg_worker] FFmpeg started (MAG322 640x360, 25 FPS)", flush=True)
                    except Exception as e:
                        ffmpeg_proc = None
                        print(f"[ffmpeg_worker] Start error: {e}", flush=True)
        else:
            with ffmpeg_lock:
                if ffmpeg_proc is not None and ffmpeg_proc.poll() is None:
                    print("[ffmpeg_worker] Stopping FFmpeg for account login", flush=True)
                    ffmpeg_proc.terminate()
                    try:
                        ffmpeg_proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        ffmpeg_proc.kill()
                        ffmpeg_proc.wait(timeout=2)
                ffmpeg_proc = None
        time.sleep(0.5)


@app.route("/stream", methods=["GET"])
def stream_video():
    """Трансляция видеопотока из локального UDP-порта для MAG322."""
    def generate():
        # Создаем стандартный UDP сокет
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Разрешаем повторное использование порта несколькими клиентами, если необходимо
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            sock.bind(("127.0.0.1", 9999))
            sock.settimeout(2.0)  # Таймаут, если FFmpeg вдруг перестал слать кадры
        except Exception as e:
            print(f"[stream] Port bind error (maybe another client connected): {e}", flush=True)
            return

        try:
            while True:
                try:
                    # Читаем UDP пакеты стандартного для MPEG-TS размера
                    data, _ = sock.recvfrom(65535)
                    if not data:
                        break
                    yield data
                except socket.timeout:
                    continue
        finally:
            sock.close()

    return Response(
        generate(), status=200,
        headers={
            "Content-Type": "video/mp2t",
            "Cache-Control": "no-store",
            "Accept-Ranges": "none"
        },
        direct_passthrough=True,
    )



@app.route("/viewer_ping", methods=["POST", "GET"])
def viewer_ping():
    global viewer_last_seen
    viewer_last_seen = time.time()
    return jsonify({"ok": True, "mode": get_mode()})


@app.route("/status", methods=["GET"])
def server_status():
    return jsonify({"ok": True, "mode": get_mode(), "viewerActive": viewer_is_active()})


@app.route("/toggle_mode", methods=["POST", "GET"])
def toggle_mode():
    target = "login" if get_mode() == "mag" else "mag"
    set_mode_value(target)
    browser_task_queue.put(("mode", target))
    if target == "login":
        # FFmpeg worker остановит процесс; очистим старые скриншоты до первого нового кадра.
        global last_screenshot_bytes
        last_screenshot_bytes = b""
    return jsonify({"ok": True, "mode": target})


@app.route("/mouse_click", methods=["GET"])
def mouse_click():
    if get_mode() != "login":
        return jsonify({"ok": False, "error": "Управление доступно только в режиме входа"}), 403
    try:
        x = float(request.args.get("x", 0)); y = float(request.args.get("y", 0))
        browser_action_queue.put(("click", x, y))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/mouse_move", methods=["GET"])
def mouse_move():
    if get_mode() != "login":
        return jsonify({"ok": False, "error": "Управление доступно только в режиме входа"}), 403
    try:
        x = float(request.args.get("x", 0)); y = float(request.args.get("y", 0))
        browser_action_queue.put(("move", x, y))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/keyboard_type", methods=["GET"])
def keyboard_type():
    if get_mode() != "login":
        return jsonify({"ok": False, "error": "Клавиатура доступна только в режиме входа"}), 403
    try:
        value = request.args.get("text", "")
        if len(value) > 2000:
            return jsonify({"ok": False, "error": "Input too long"}), 413
        browser_action_queue.put(("type", value))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/", methods=["GET"])
def index():
    html_page = r"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
      <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
      <title>YouTube MAG322 Remote Control</title>
      <style>
        body{font-family:Arial,sans-serif;background:#121212;color:#fff;text-align:center;padding:10px}
        .card{background:#1e1e1e;padding:15px;border-radius:10px;display:inline-block;box-shadow:0 4px 10px #0008;width:840px;max-width:96%;box-sizing:border-box}
        input,button{padding:9px;margin:5px 0;border-radius:5px;border:1px solid #444;background:#2c2c2c;color:#fff;font-size:14px}
        button{background:#d00;cursor:pointer;border:0}button:hover{filter:brightness(.85)}
        .screen-container{position:relative;display:inline-block;margin-top:10px;border:2px solid #444;overflow:hidden;max-width:100%}
        #liveScreenshot { width: 800px; height: 450px; object-fit: contain; display: block; background: #000; cursor: none; }
        .control-row{margin:10px 0}.hidden{display:none!important}
        #virtualCursor{position:absolute;width:12px;height:12px;background:#f00c;border:2px solid #fff;border-radius:50%;pointer-events:none;transform:translate(-50%,-50%);display:none;z-index:10}
        #modeButton{display:block;width:100%;margin:22px auto 5px;padding:13px;font-size:16px;font-weight:bold;background:#1976d2}
        .key-buttons button{min-width:48px}
      </style>
    </head>
    <body>
      <div class="card">
        <h2>🎦 Пульт управления MAG322</h2>
        <div class="screen-container" id="screenContainer">
          <div id="virtualCursor"></div>
          <img id="liveScreenshot" src="/screenshot" alt="Экран браузера">
        </div>

        <div id="magControls">
          <h3>📺 YouTube для MAG322</h3>
          <input type="text" id="videoInput" placeholder="ID видео или ссылка YouTube" style="width:75%;max-width:520px">
          <br><button onclick="switchVideo()">Включить на приставке</button>
        </div>

        <div id="loginControls" class="hidden">
          <hr style="border-color:#444;margin:18px 0">
          <h3>🔑 Вход в Google / YouTube</h3>
          <div class="control-row">
            <input type="text" id="typeInput" placeholder="Введите текст для браузера..." style="width:70%;max-width:500px">
            <button onclick="sendText()">Ввести</button>
          </div>
          <div class="key-buttons">
            <button onclick="sendKey('Enter')">Enter</button><button onclick="sendKey('Tab')">Tab</button>
            <button onclick="sendKey('Escape')">Esc</button><button onclick="sendKey('Backspace')">⌫</button>
            <button onclick="sendKey('ArrowUp')">↑</button><button onclick="sendKey('ArrowDown')">↓</button>
            <button onclick="sendKey('ArrowLeft')">←</button><button onclick="sendKey('ArrowRight')">→</button>
          </div>
          <p style="color:#aaa;font-size:13px">Нажимайте на изображение для клика и перемещайте мышь для движения курсора.</p>
        </div>

        <p id="status" style="margin-top:12px;font-weight:bold"></p>
        <button id="modeButton" onclick="toggleMode()">1. Открыть YouTube для входа</button>
      </div>

      <script>
        const img=document.getElementById('liveScreenshot');
        const cursor=document.getElementById('virtualCursor');
        const screen=document.getElementById('screenContainer');
        const statusEl=document.getElementById('status');
        let mode='mag', pollTimer=null, heartbeatTimer=null, lastMoveAt=0;
 
        function setModeUI(next){
          mode=next;
          const login=mode==='login';
          document.getElementById('magControls').classList.toggle('hidden',login);
          document.getElementById('loginControls').classList.toggle('hidden',!login);
          document.getElementById('modeButton').textContent=login?'2. Вернуть плеер для MAG322':'1. Открыть YouTube для входа';
          screen.style.cursor=login?'crosshair':'default';
          cursor.style.display='none';
          img.style.cursor=login?'crosshair':'default';
          img.style.aspectRatio=login?'854 / 480':'16 / 9';
        }
        async function syncStatus(){
          try{const r=await fetch('/status',{cache:'no-store'});const d=await r.json();setModeUI(d.mode||'mag');}catch(e){}
        }
        async function heartbeat(){
          if(document.visibilityState==='visible'){
            try{await fetch('/viewer_ping',{method:'POST',cache:'no-store'});}catch(e){}
          }
        }
        function startPolling(){
          if(pollTimer)clearInterval(pollTimer);
          pollTimer=setInterval(async ()=>{
            if(document.visibilityState==='visible') {
              try {
                const res = await fetch('/screenshot?t=' + Date.now());
                if (res.ok) {
                  const blob = await res.blob();
                  if (blob.size > 0) {
                    if (img.dataset.objectUrl) {
                      URL.revokeObjectURL(img.dataset.objectUrl);
                    }
                    const url = URL.createObjectURL(blob);
                    img.dataset.objectUrl = url;
                    img.src = url;
                  }
                }
              } catch (e) {
                // Ошибку сети или пустой ответ игнорируем, старый кадр остается
              }
            }
          }, mode==='login'?200:500);
          
          if(heartbeatTimer)clearInterval(heartbeatTimer);
          heartbeatTimer=setInterval(heartbeat,4000);
          heartbeat();
        }
        document.addEventListener('visibilitychange',()=>{
          if(document.visibilityState==='visible'){heartbeat();img.src='/screenshot?t='+Date.now();}
        });
        async function toggleMode(){
          const b=document.getElementById('modeButton');b.disabled=true;
          statusEl.textContent='Переключение режима...';
          try{
            const r=await fetch('/toggle_mode',{method:'POST'});const d=await r.json();
            if(!d.ok)throw new Error(d.error||'Не удалось переключить режим');
            setModeUI(d.mode);startPolling();
            statusEl.textContent=d.mode==='login'?'Открыт YouTube. Можно войти в аккаунт.':'Возврат к плееру MAG322.';
          }catch(e){statusEl.textContent='Ошибка: '+e.message;}
          finally{b.disabled=false;}
        }
        function coords(ev){
          const r=img.getBoundingClientRect();
          const w=mode==='login'?854:640, h=mode==='login'?480:360;
          return {x:Math.max(0,Math.min(w,Math.round((ev.clientX-r.left)/r.width*w))),y:Math.max(0,Math.min(h,Math.round((ev.clientY-r.top)/r.height*h)))};
        }
        screen.addEventListener('mousemove',ev=>{
          if(mode!=='login')return;
          const r=img.getBoundingClientRect();cursor.style.left=(ev.clientX-r.left)+'px';cursor.style.top=(ev.clientY-r.top)+'px';cursor.style.display='block';
          const now=Date.now();if(now-lastMoveAt<35)return;lastMoveAt=now;
          const p=coords(ev);fetch(`/mouse_move?x=${p.x}&y=${p.y}`).catch(()=>{});
        });
        screen.addEventListener('mouseleave',()=>cursor.style.display='none');
        img.addEventListener('click',async ev=>{
          if(mode!=='login')return;
          const p=coords(ev);try{await fetch(`/mouse_click?x=${p.x}&y=${p.y}`);img.src='/screenshot?t='+Date.now();}catch(e){}
        });
        async function sendText(){
          const input=document.getElementById('typeInput');if(!input.value)return;
          const r=await fetch('/keyboard_type?text='+encodeURIComponent(input.value));const d=await r.json();
          if(d.ok)input.value='';else statusEl.textContent=d.error||'Ошибка ввода';
          setTimeout(()=>img.src='/screenshot?t='+Date.now(),150);
        }
        async function sendKey(k){
          const val='['+k.toUpperCase()+']';
          const r=await fetch('/keyboard_type?text='+encodeURIComponent(val));const d=await r.json();
          if(!d.ok)statusEl.textContent=d.error||'Ошибка клавиши';
          setTimeout(()=>img.src='/screenshot?t='+Date.now(),100);
        }
        document.addEventListener('keydown',ev=>{
          if(mode!=='login'||ev.target===document.getElementById('typeInput')||ev.target===document.getElementById('videoInput'))return;
          const keys={Enter:'ENTER',Tab:'TAB',Escape:'ESC',Backspace:'BACKSPACE',' ':'SPACE',ArrowUp:'UP',ArrowDown:'DOWN',ArrowLeft:'LEFT',ArrowRight:'RIGHT',Home:'HOME',End:'END',PageUp:'PAGEUP',PageDown:'PAGEDOWN',Delete:'DELETE'};
          if(keys[ev.key]){ev.preventDefault();sendKey(keys[ev.key]);}
        });
        async function switchVideo(){
          let input=document.getElementById('videoInput').value.trim(),id=input;
          try{if(input.includes('youtube.com'))id=new URL(input).searchParams.get('v')||'';else if(input.includes('youtu.be/'))id=input.split('youtu.be/')[1].split(/[?&]/)[0];}catch(e){}
          if(!id){alert('Введите ID или ссылку YouTube');return;}
          statusEl.textContent='Переключаем видео...';
          try{const r=await fetch('/get_stream?id='+encodeURIComponent(id));const d=await r.json();statusEl.textContent=d.ok?'Готово! Видео обновлено.':(d.error||'Ошибка');}
          catch(e){statusEl.textContent='Ошибка связи с сервером';}
        }
        syncStatus().then(startPolling);
      </script>
    </body>
    </html>
    """
    return render_template_string(html_page)

if __name__ == "__main__":
    # 1. Поток управления браузером
    browser_thread = threading.Thread(target=browser_worker_loop, daemon=True)
    browser_thread.start()

    # 2. FFmpeg включается только в режиме MAG322
    ffmpeg_thread = threading.Thread(target=ffmpeg_worker_loop, daemon=True)
    ffmpeg_thread.start()

    time.sleep(3)
    try:
        app.run(host="0.0.0.0", port=6901, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        # Корректная зачистка при выходе
        if browser: browser.close()
        if pw: pw.stop()
        if ffmpeg_proc and ffmpeg_proc.poll() is None: ffmpeg_proc.terminate()
        if xvfb_proc: xvfb_proc.terminate()
        if os.path.exists(FIFO_PATH): os.remove(FIFO_PATH)
