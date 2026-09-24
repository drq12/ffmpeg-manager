import json, os, re, signal, subprocess, threading, time, uuid, collections, shlex
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

DATA = os.environ.get("DATA_DIR", "/data")
PROVIDER = os.environ.get("SERVICE_PROVIDER", "axioma")
FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")
# ffmpeg/ffprobe/nvidia-smi run in the host mount namespace (system ffmpeg).
# Empty HOST_EXEC value = run inside the container instead.
HOST_EXEC = shlex.split(os.environ.get("HOST_EXEC", "nsenter -t 1 -m --"))
NO_PROGRESS_TIMEOUT = 20   # seconds without ffmpeg progress -> consider the stream lost
FALLBACK_RECHECK = 5       # seconds between attempts to detect a recovered source
os.makedirs(DATA, exist_ok=True)
CFG = os.path.join(DATA, "channels.json")
R = {}  # id -> Runner

LANGS = ("ru", "en", "be", "ja")
LANG_UI = os.environ.get("LANG_UI", "ru").lower()
if LANG_UI not in LANGS:
    LANG_UI = "ru"

# UI strings used server-side (Runner log entries and validation errors). The rest of the
# interface is translated client-side in index.html; this dict only covers backend-generated text.
L = {
    "log_failed_start": {"ru": "не удалось запустить: {e}", "en": "failed to start: {e}",
                          "be": "не ўдалося запусціць: {e}", "ja": "起動できませんでした: {e}"},
    "log_mem_limit": {"ru": "память {rss} МБ > лимита {lim} МБ, перезапуск",
                       "en": "memory {rss} MB > limit {lim} MB, restarting",
                       "be": "памяць {rss} МБ > ліміту {lim} МБ, перазапуск",
                       "ja": "メモリ使用量 {rss} MB が上限 {lim} MB を超えました。再起動します"},
    "log_no_input": {"ru": "нет входного потока {n}с, перезапуск", "en": "no input stream for {n}s, restarting",
                      "be": "няма ўваходнага патоку {n}с, перазапуск",
                      "ja": "{n}秒間、入力ストリームがありません。再起動します"},
    "log_fallback_on": {"ru": "источник недоступен, включена заставка (SMPTE bars)",
                         "en": "source unavailable, test pattern enabled (SMPTE bars)",
                         "be": "крыніца недаступная, уключана застаўка (SMPTE bars)",
                         "ja": "ソースが利用できません。テストパターン（SMPTEバー）を有効にしました"},
    "log_fallback_recovered": {"ru": "источник снова доступен, возвращаемся к нему",
                                "en": "source available again, switching back",
                                "be": "крыніца зноў даступная, вяртаемся да яе",
                                "ja": "ソースが復旧しました。元のストリームに戻ります"},
    "log_fallback_proc_died": {"ru": "процесс заставки неожиданно завершился, перезапуск заставки",
                                "en": "test pattern process exited unexpectedly, restarting it",
                                "be": "працэс застаўкі нечакана завяршыўся, перазапуск застаўкі",
                                "ja": "テストパターンのプロセスが予期せず終了しました。再起動します"},
    "log_fallback_start_failed": {"ru": "не удалось запустить заставку: {e}",
                                   "en": "failed to start test pattern: {e}",
                                   "be": "не ўдалося запусціць застаўку: {e}",
                                   "ja": "テストパターンを起動できませんでした: {e}"},
    "log_fallback_restart_failed": {"ru": "не удалось перезапустить заставку: {e}",
                                     "en": "failed to restart test pattern: {e}",
                                     "be": "не ўдалося перазапусціць застаўку: {e}",
                                     "ja": "テストパターンを再起動できませんでした: {e}"},
    "log_restart_delay": {"ru": "ffmpeg завершился (код {code}), перезапуск через {delay}с",
                           "en": "ffmpeg exited (code {code}), restarting in {delay}s",
                           "be": "ffmpeg завяршыўся (код {code}), перазапуск праз {delay}с",
                           "ja": "ffmpegが終了しました（コード {code}）。{delay}秒後に再起動します"},
    "log_scheduled_restart": {"ru": "плановый перезапуск по расписанию ({hhmm})",
                               "en": "scheduled restart ({hhmm})",
                               "be": "плановы перазапуск па раскладзе ({hhmm})",
                               "ja": "スケジュールによる再起動 ({hhmm})"},
    "err_channel_fields": {"ru": "нужны имя, вход и минимум один выход с битрейтом",
                            "en": "name, input and at least one output with bitrate are required",
                            "be": "патрэбныя назва, уваход і прынамсі адзін выхад з бітрэйтам",
                            "ja": "名前、入力、そしてビットレートを持つ出力が少なくとも1つ必要です"},
    "err_preset": {"ru": "preset должен быть p1..p7", "en": "preset must be p1..p7",
                   "be": "preset павінен быць p1..p7", "ja": "presetはp1〜p7で指定してください"},
    "err_bframes": {"ru": "B-кадры: допустимо 0 или 2", "en": "B-frames: only 0 or 2 allowed",
                     "be": "B-кадры: дапушчальна 0 або 2", "ja": "Bフレームは0または2のみ指定できます"},
    "err_scale_mode": {"ru": "scale_mode должен быть cpu, gpu, decoder или none",
                        "en": "scale_mode must be cpu, gpu, decoder or none",
                        "be": "scale_mode павінен быць cpu, gpu, decoder або none",
                        "ja": "scale_modeはcpu、gpu、decoder、noneのいずれかです"},
    "err_schedule_type": {"ru": "расписание должно быть списком времени", "en": "schedule must be a list of times",
                           "be": "расклад павінен быць спісам часу", "ja": "スケジュールは時刻のリストである必要があります"},
    "err_schedule_format": {"ru": "неверный формат времени: «{t}» (ожидается ЧЧ:ММ)",
                             "en": 'invalid time format: "{t}" (expected HH:MM)',
                             "be": "няправільны фармат часу: «{t}» (чакаецца ГГ:ХХ)",
                             "ja": "時刻の形式が正しくありません: 「{t}」（HH:MM形式で入力してください）"},
    "err_import_empty": {"ru": "файл импорта пуст или имеет неверный формат",
                          "en": "import file is empty or has an invalid format",
                          "be": "файл імпарту пусты або мае няправільны фармат",
                          "ja": "インポートファイルが空か、形式が正しくありません"},
    "err_import_item": {"ru": "канал #{i}: {e}", "en": "channel #{i}: {e}",
                         "be": "канал #{i}: {e}", "ja": "チャンネル #{i}: {e}"},
    "err_probe_no_video": {"ru": "видеодорожка не найдена", "en": "no video stream found",
                            "be": "відэадарожка не знойдзена", "ja": "映像ストリームが見つかりません"},
    "err_probe_timeout": {"ru": "нет ответа от потока (таймаут)", "en": "no response from stream (timeout)",
                           "be": "няма адказу ад патоку (таймаут)", "ja": "ストリームからの応答がありません（タイムアウト）"},
    "err_probe_unavailable": {"ru": "поток недоступен", "en": "stream unavailable",
                               "be": "паток недаступны", "ja": "ストリームを利用できません"},
}


def t(key, **kw):
    entry = L.get(key, {})
    tpl = entry.get(LANG_UI) or entry.get("en") or key
    try:
        return tpl.format(**kw)
    except Exception:
        return tpl


def in_url(x):
    return x if "://" in x else f"udp://@{x}?fifo_size=1000000&overrun_nonfatal_option=1"


def out_url(o):
    x = o["url"]
    if "://" in x:
        return x
    if o.get("mux"):
        mux = int(o["mux"])
    else:
        vb = int(o.get("vb") or 0); ab = int(o.get("ab") or 96)
        mux = round((vb + ab) * 1150)  # auto: (video+audio) kbit + 15% headroom for TS/UDP overhead, to bit/s
    return f"udp://@{x}?fifo_buffer=1000000&overrun_nonfatal_option=1&pkt_size=1316&ttl=6&bitrate={mux}"


def output_args(o, c, vmap, amap, force_cpu_scale=False):
    """Common tail for one output: audio + video (with NVENC optimizations) + metadata + destination address."""
    vb = int(o["vb"]); mr = int(o.get("maxrate") or vb); bs = int(o.get("bufsize") or mr * 2)
    a = ["-map", amap, "-acodec", "aac", "-ac", "2", "-b:a", f'{int(o.get("ab") or 96)}k', "-map", vmap]
    mode = c.get("scale_mode", "cpu")
    if force_cpu_scale:
        if o.get("height"):
            a += ["-vf", f'scale=-2:{int(o["height"])}']
    elif mode == "decoder":
        pass  # scaling already set once at the decoder (-resize) for the whole channel, see build_cmd
    elif mode == "none":
        pass  # scaling disabled - output keeps the decoder's native resolution, height field is ignored
    elif o.get("height"):
        # scale_cuda scales directly in GPU memory (the frame is already there after hwaccel cuda decode),
        # avoiding the extra GPU->CPU->GPU copy that a regular software scale filter would need.
        a += ["-vf", f'{"scale_cuda" if mode == "gpu" else "scale"}=-2:{int(o["height"])}']
    preset = c.get("preset") or "p4"
    bf = int(c.get("bframes") or 0)
    a += ["-vcodec", "h264_nvenc", "-preset", preset, "-profile:v", "main", "-level", "4.1", "-pix_fmt", "yuv420p",
          "-g", "15", "-keyint_min", "15", "-sc_threshold", "0", "-bf", str(bf),
          "-rc", "cbr", "-rc-lookahead", "8", "-tune", "ll",
          "-b:v", f"{vb}k", "-maxrate", f"{mr}k", "-bufsize", f"{bs}k"]
    if c.get("aq", True):
        a += ["-spatial-aq", "1", "-temporal-aq", "1", "-aq-strength", "8"]
    a += ["-f", "mpegts", "-flush_packets", "1", "-muxdelay", "0", "-muxpreload", "0", "-pcr_period", "20",
          "-metadata", f"service_provider={PROVIDER}", "-metadata", f'service_name={o.get("name") or c["name"]}',
          out_url(o)]
    return a


def build_cmd(c):
    a = [FFMPEG, "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-isync", "0", "-y",
         # resilience to jitter/corrupt packets on receive, and a faster start
         "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
         "-probesize", "1000000", "-analyzeduration", "1000000", "-thread_queue_size", "4096",
         "-hwaccel_device", str(c.get("gpu", 0)), "-hwaccel_output_format", "cuda", "-c:v", "h264_cuvid",
         "-deint", "1", "-drop_second_field", "1"]
    if c.get("scale_mode") == "decoder" and c.get("resize_w") and c.get("resize_h"):
        # scale right at the cuvid decoder: once for the whole channel, no separate -vf per output.
        # Only works if every output of the channel shares the same resolution (use cpu/gpu mode otherwise).
        a += ["-resize", f'{int(c["resize_w"])}x{int(c["resize_h"])}']
    a += ["-vsync", "1", "-re", "-i", in_url(c["input"])]
    for o in c["outputs"]:
        a += output_args(o, c, "0:v:0", "0:a:0")
    return a


def build_fallback_cmd(c):
    """Test pattern (SMPTE bars) + tone to all of the channel's outputs while there is no source signal."""
    a = [FFMPEG, "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-re",
         "-f", "lavfi", "-i", "smptebars=size=1280x720:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000"]
    for o in c["outputs"]:
        a += output_args(o, c, "0:v", "1:a", force_cpu_scale=True)  # lavfi produces software frames, not cuda
    return a


def probe_ok(inp, timeout=4):
    try:
        r = subprocess.run(HOST_EXEC + [FFPROBE, "-v", "error", "-rw_timeout", "3000000",
                            "-analyzeduration", "1500000", "-probesize", "1500000",
                            "-select_streams", "v:0", "-show_entries", "stream=codec_type",
                            "-of", "json", in_url(inp)],
                            capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0 and '"codec_type"' in r.stdout
    except Exception:
        return False


class Runner:
    def __init__(s, c):
        s.c = c; s.state = "stopped"; s.stop_ev = threading.Event(); s.proc = None; s.fb_proc = None
        s.log = collections.deque(maxlen=30); s.stats = {}; s.restarts = 0
        s.started = 0; s.last = 0; s.th = None; s.rss = 0; s.over = 0; s.manual_stop = False
        s.last_restart = 0  # time of the last unplanned restart (0 = none yet)
        s.last_sched_fired = None  # "YYYY-MM-DD HH:MM" of the last schedule trigger (prevents repeats)

    def start(s):
        if s.th and s.th.is_alive():
            return
        s.stop_ev.clear(); s.restarts = 0; s.manual_stop = False
        s.th = threading.Thread(target=s.loop, daemon=True); s.th.start()

    def stop(s):
        s.manual_stop = True; s.stop_ev.set()
        for p in (s.proc, s.fb_proc):
            if p and p.poll() is None:
                p.terminate()
        if s.th:
            s.th.join(5)
            if s.th.is_alive():
                for p in (s.proc, s.fb_proc):
                    if p and p.poll() is None:
                        p.kill()
                s.th.join(3)
        s.state = "stopped"

    def watch(s, p):
        # watch memory usage and progress of the main process; kill on trouble, loop() handles the rest
        s.over = 0
        while p.poll() is None:
            time.sleep(2)
            s.rss = rss_mb(p.pid)
            lim = int(s.c.get("mem_limit") or 0)
            s.over = s.over + 1 if lim and s.rss > lim else 0  # 2 checks in a row (~4s) to ignore brief spikes
            if s.over >= 2 and p.poll() is None:
                s.log.append(t("log_mem_limit", rss=s.rss, lim=lim)); p.kill(); break
            if time.time() - s.last > NO_PROGRESS_TIMEOUT and p.poll() is None:
                s.log.append(t("log_no_input", n=NO_PROGRESS_TIMEOUT)); p.kill(); break
        s.rss = 0

    def run_fallback(s):
        s.state = "fallback"
        s.log.append(t("log_fallback_on"))
        try:
            s.fb_proc = p = subprocess.Popen(HOST_EXEC + build_fallback_cmd(s.c),
                                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except Exception as e:
            s.log.append(t("log_fallback_start_failed", e=e)); return
        threading.Thread(target=lambda: [s.log.append(l.strip()) for l in p.stderr], daemon=True).start()
        while not s.stop_ev.is_set():
            if probe_ok(s.c["input"]):
                s.log.append(t("log_fallback_recovered")); break
            if p.poll() is not None:
                s.log.append(t("log_fallback_proc_died"))
                try:
                    s.fb_proc = p = subprocess.Popen(HOST_EXEC + build_fallback_cmd(s.c),
                                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
                    threading.Thread(target=lambda: [s.log.append(l.strip()) for l in p.stderr], daemon=True).start()
                except Exception as e:
                    s.log.append(t("log_fallback_restart_failed", e=e)); time.sleep(FALLBACK_RECHECK)
            s.stop_ev.wait(FALLBACK_RECHECK)
        if p.poll() is None:
            p.terminate()
            try: p.wait(5)
            except Exception: p.kill()
        s.fb_proc = None

    def loop(s):
        delay = 2
        while not s.stop_ev.is_set():
            s.state = "starting"; s.stats = {}
            try:
                s.proc = p = subprocess.Popen(HOST_EXEC + build_cmd(s.c), stdout=subprocess.PIPE,
                                               stderr=subprocess.PIPE, text=True, bufsize=1)
            except Exception as e:
                s.log.append(t("log_failed_start", e=e)); s.state = "restarting"; s.stop_ev.wait(10); continue
            s.started = s.last = time.time()
            threading.Thread(target=lambda: [s.log.append(l.strip()) for l in p.stderr], daemon=True).start()
            threading.Thread(target=s.watch, args=(p,), daemon=True).start()
            for l in p.stdout:
                k, _, v = l.strip().partition("=")
                if k in ("fps", "bitrate", "speed", "out_time", "frame"):
                    s.stats[k] = v
                elif k == "progress":
                    s.last = time.time(); s.state = "running"
            p.wait()
            if s.stop_ev.is_set() or s.manual_stop:
                break
            # any unplanned exit of the main process counts as a restart
            s.restarts += 1; s.last_restart = time.time()
            no_input = time.time() - s.started < NO_PROGRESS_TIMEOUT + 5  # exited fast / likely no signal
            if s.c.get("fallback") and no_input:
                s.run_fallback()
                if s.stop_ev.is_set():
                    break
                delay = 2  # source is back - try the real input right away, no growing backoff
                continue
            delay = 2 if time.time() - s.started > 60 else min(delay * 2, 30)
            s.state = "restarting"
            s.log.append(t("log_restart_delay", code=p.returncode, delay=delay))
            s.stop_ev.wait(delay)
        s.state = "stopped"

    def view(s):
        up = int(time.time() - s.started) if s.state in ("running", "fallback") else 0
        since_restart = int(time.time() - s.last_restart) if s.last_restart else None
        return {**s.c, "state": s.state, "restarts": s.restarts, "uptime": up, "rss": s.rss,
                "since_restart": since_restart, "stats": s.stats, "log": list(s.log),
                "cmd": " ".join(shlex.quote(x) for x in build_cmd(s.c))}


def save():
    json.dump([r.c for r in R.values()], open(CFG, "w"), ensure_ascii=False, indent=1)


PRESETS = {"p1", "p2", "p3", "p4", "p5", "p6", "p7"}
SCALE_MODES = {"cpu", "gpu", "decoder", "none"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_choice(name, default, allowed):
    v = os.environ.get(name, default)
    return v if v in allowed else default


# Default values for the SD/HD template buttons in the "add channel" wizard - overridable via docker-compose
TEMPLATES = {
    "sd": {
        "mem_limit": env_int("SD_MEM_LIMIT", 550),
        "preset": env_choice("SD_PRESET", "p4", PRESETS),
        "scale_mode": env_choice("SD_SCALE_MODE", "cpu", SCALE_MODES),
        "bitrate": env_int("SD_BITRATE", 1000),
        "maxrate": env_int("SD_MAXRATE", 1000),
        "width": env_int("SD_WIDTH", 600),
        "height": env_int("SD_HEIGHT", 480),
    },
    "hd": {
        "mem_limit": env_int("HD_MEM_LIMIT", 650),
        "preset": env_choice("HD_PRESET", "p1", PRESETS),
        "scale_mode": env_choice("HD_SCALE_MODE", "decoder", SCALE_MODES),
        "bitrate": env_int("HD_BITRATE", 2000),
        "maxrate": env_int("HD_MAXRATE", 2000),
        "width": env_int("HD_WIDTH", 1280),
        "height": env_int("HD_HEIGHT", 720),
    },
}


def clean_schedule(lst):
    if not isinstance(lst, list):
        raise ValueError(t("err_schedule_type"))
    out = []
    for tm in lst:
        tm = str(tm).strip()
        if not TIME_RE.match(tm):
            raise ValueError(t("err_schedule_format", t=tm))
        if tm not in out:
            out.append(tm)
    return sorted(out)


def scheduler_loop():
    """Every 20s, checks channel schedules and restarts any channel whose time has come."""
    while True:
        now = time.localtime(); stamp = time.strftime("%Y-%m-%d %H:%M", now); hhmm = stamp[-5:]
        for r in list(R.values()):
            if hhmm in (r.c.get("schedule") or []) and r.c.get("enabled") and r.last_sched_fired != stamp:
                r.last_sched_fired = stamp
                r.log.append(t("log_scheduled_restart", hhmm=hhmm))
                threading.Thread(target=lambda rr=r: (rr.stop(), rr.start()), daemon=True).start()
        time.sleep(20)


def clean(b):
    outs = [o for o in b.get("outputs", []) if o.get("url") and o.get("vb")]
    if not b.get("name") or not b.get("input") or not outs:
        raise ValueError(t("err_channel_fields"))
    preset = b.get("preset") or "p4"
    if preset not in PRESETS:
        raise ValueError(t("err_preset"))
    bf = int(b.get("bframes") or 0)
    if bf not in (0, 2):
        raise ValueError(t("err_bframes"))
    mode = b.get("scale_mode") or "cpu"
    if mode not in SCALE_MODES:
        raise ValueError(t("err_scale_mode"))
    return {"name": b["name"].strip(), "input": b["input"].strip(), "gpu": int(b.get("gpu") or 0),
            "mem_limit": int(b.get("mem_limit") or 0), "fallback": bool(b.get("fallback")),
            "preset": preset, "aq": bool(b.get("aq", True)), "bframes": bf,
            "scale_mode": mode, "resize_w": int(b.get("resize_w") or 0), "resize_h": int(b.get("resize_h") or 0),
            "schedule": clean_schedule(b.get("schedule") or []), "outputs": outs}


def rss_mb(pid):
    try:
        for l in open(f"/proc/{pid}/status"):
            if l.startswith("VmRSS"):
                return int(l.split()[1]) // 1024
    except OSError:
        pass
    return 0


_cpu = None
def cpu():
    global _cpu
    v = list(map(int, open("/proc/stat").readline().split()[1:8])); t = sum(v); i = v[3] + v[4]; r = 0
    if _cpu and t != _cpu[0]:
        r = 100 * (1 - (i - _cpu[1]) / (t - _cpu[0]))
    _cpu = (t, i); return round(r, 1)


def mem():
    m = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
    return {"total": m["MemTotal"] // 1024, "used": (m["MemTotal"] - m["MemAvailable"]) // 1024}


def gpus():
    try:
        out = subprocess.check_output(HOST_EXEC + ["nvidia-smi",
                                      "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                                      "--format=csv,noheader,nounits"], text=True, timeout=5)
        return [dict(zip(("i", "name", "util", "mu", "mt", "temp"), [x.strip() for x in l.split(",")]))
                for l in out.strip().splitlines()]
    except Exception:
        return []


def probe(inp):
    try:
        r = subprocess.run(HOST_EXEC + [FFPROBE, "-v", "error", "-rw_timeout", "5000000",
                            "-analyzeduration", "3000000", "-probesize", "3000000",
                            "-show_entries", "stream=codec_type,codec_name,width,height,bit_rate,r_frame_rate:format=bit_rate",
                            "-of", "json", in_url(inp)],
                            capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or t("err_probe_unavailable")).strip().splitlines()[-1][:200]}
        d = json.loads(r.stdout or "{}")
        streams = d.get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not v:
            return {"ok": False, "error": t("err_probe_no_video")}
        fps = None
        if v.get("r_frame_rate") and "/" in v["r_frame_rate"]:
            n, _, dd = v["r_frame_rate"].partition("/")
            if int(dd or 1):
                fps = round(int(n) / int(dd), 2)
        return {"ok": True,
                "video": {"codec": v.get("codec_name"), "width": v.get("width"), "height": v.get("height"),
                          "fps": fps},
                "audio": {"codec": a.get("codec_name"), "bitrate": int(a["bit_rate"]) // 1000 if a and a.get("bit_rate") else None} if a else None}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": t("err_probe_timeout")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class H(BaseHTTPRequestHandler):
    def j(s, obj, code=200):
        d = json.dumps(obj, ensure_ascii=False).encode()
        s.send_response(code); s.send_header("Content-Type", "application/json"); s.end_headers(); s.wfile.write(d)

    def do_GET(s):
        if s.path == "/api/state":
            return s.j({"channels": [r.view() for r in R.values()], "templates": TEMPLATES,
                        "sys": {"cpu": cpu(), "mem": mem(), "gpus": gpus(),
                        "ffmpeg_rss": sum(r.rss for r in R.values())}})
        if s.path == "/api/export":
            d = json.dumps([r.c for r in R.values()], ensure_ascii=False, indent=1).encode()
            s.send_response(200); s.send_header("Content-Type", "application/json")
            s.send_header("Content-Disposition", 'attachment; filename="channels_export.json"')
            s.end_headers(); s.wfile.write(d); return
        s.send_response(200); s.send_header("Content-Type", "text/html; charset=utf-8"); s.end_headers()
        html = open(os.path.join(os.path.dirname(__file__), "index.html"), encoding="utf-8").read()
        html = html.replace("<!--LANG_PLACEHOLDER-->", f'<script>window.APP_LANG={json.dumps(LANG_UI)};</script>')
        s.wfile.write(html.encode("utf-8"))

    def route(s, m):
        p = s.path.strip("/").split("/")
        n = int(s.headers.get("Content-Length", 0)); b = json.loads(s.rfile.read(n) or "{}") if n else {}
        try:
            if m == "POST" and p[1] == "probe":
                return s.j(probe(b.get("input", "")))
            if m == "POST" and p[1] == "import":
                items = b if isinstance(b, list) else b.get("channels", [])
                if not isinstance(items, list) or not items:
                    raise ValueError(t("err_import_empty"))
                added = []
                for i, item in enumerate(items):
                    try:
                        c = clean(item)
                    except Exception as e:
                        raise ValueError(t("err_import_item", i=i + 1, e=e))
                    c["id"] = uuid.uuid4().hex[:8]; c["enabled"] = False  # imported channels are always disabled
                    added.append(c)
                for c in added:
                    R[c["id"]] = Runner(c)
                save(); return s.j({"ok": 1, "imported": len(added)})
            if m == "POST" and len(p) == 2:
                c = clean(b); c["id"] = uuid.uuid4().hex[:8]; c["enabled"] = False  # a new channel starts disabled
                R[c["id"]] = Runner(c)
            else:
                r = R[p[2]]
                if m == "PUT":
                    c = clean(b)
                    if "schedule" not in b:  # a full edit via the wizard does not touch the schedule
                        c["schedule"] = r.c.get("schedule", [])
                    was_on = r.c.get("enabled", False); r.stop(); r.c.update(c); r.c["enabled"] = was_on
                    if was_on: r.start()
                elif m == "DELETE":
                    r.stop(); del R[p[2]]
                elif p[3] == "start": r.c["enabled"] = True; r.start()
                elif p[3] == "stop": r.c["enabled"] = False; r.stop()
                elif p[3] == "restart": r.stop(); r.c["enabled"] = True; r.start()
                elif p[3] == "schedule": r.c["schedule"] = clean_schedule(b.get("schedule") or [])
            save(); s.j({"ok": 1})
        except Exception as e:
            s.j({"error": str(e)}, 400)

    do_POST = lambda s: s.route("POST")
    do_PUT = lambda s: s.route("PUT")
    do_DELETE = lambda s: s.route("DELETE")

    def log_message(s, *a): pass


if __name__ == "__main__":
    if os.path.exists(CFG):
        for c in json.load(open(CFG)):
            c.setdefault("mem_limit", 0); c.setdefault("fallback", False)
            c.setdefault("preset", "p4"); c.setdefault("aq", True); c.setdefault("bframes", 0)
            if "scale_mode" not in c:  # migrate from the old boolean scale_gpu field
                c["scale_mode"] = "gpu" if c.pop("scale_gpu", False) else "cpu"
            c.setdefault("resize_w", 0); c.setdefault("resize_h", 0); c.setdefault("schedule", [])
            R[c["id"]] = r = Runner(c)
            if c.get("enabled"): r.start()
    cpu()
    threading.Thread(target=scheduler_loop, daemon=True).start()

    def bye(*a):  # docker stop: cleanly stop every ffmpeg (enabled flag in config is untouched - resumes on start)
        ts = [threading.Thread(target=r.stop) for r in R.values()]
        [t.start() for t in ts]; [t.join() for t in ts]; os._exit(0)
    signal.signal(signal.SIGTERM, bye)
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), H).serve_forever()
