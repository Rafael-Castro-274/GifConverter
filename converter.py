"""
Nucleo de conversao: aumenta o FPS de um GIF usando interpolacao de
movimento do FFmpeg (filtro minterpolate).

Nao depende de interface grafica -- pode ser usado sozinho ou importado
pela GUI (app.py).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path


class ConversionError(Exception):
    """Erro durante a conversao do GIF."""


class ConversionCancelled(ConversionError):
    """A conversao foi cancelada pelo usuario."""


# Contexto ativo da conversao atual (a GUI roda uma por vez).
# _run le daqui para cancelar, registrar o processo e reportar progresso.
_ACTIVE: dict = {"cancel": None, "on_process": None, "on_percent": None}


def _cancelled() -> bool:
    ev = _ACTIVE.get("cancel")
    return ev is not None and ev.is_set()


def _report(pct: float) -> None:
    cb = _ACTIVE.get("on_percent")
    if cb:
        cb(max(0.0, min(100.0, pct)))


def _parse_ts(stamp: str) -> float | None:
    try:
        h, m, s = stamp.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, IndexError):
        return None


def _base_dirs() -> list[Path]:
    """Pastas onde procurar binarios/recursos embutidos.

    Com PyInstaller (--onefile) os extras sao extraidos para sys._MEIPASS;
    tambem procuramos ao lado do executavel/script e em ./bin.
    """
    dirs: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass))
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent
    dirs.append(base)
    dirs.append(base / "bin")
    return dirs


def _find_bundled(*relparts: str) -> Path | None:
    """Retorna o primeiro caminho existente (arquivo ou pasta) entre as bases."""
    for d in _base_dirs():
        cand = d.joinpath(*relparts)
        if cand.exists():
            return cand
    return None


def _bundled_ffmpeg() -> str | None:
    exe_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    found = _find_bundled(exe_name)
    return str(found) if found else None


def find_ffmpeg() -> str:
    """Retorna o caminho do executavel ffmpeg ou levanta ConversionError.

    Prioridade: ffmpeg embutido (no .exe) -> ffmpeg no PATH do sistema.
    """
    bundled = _bundled_ffmpeg()
    if bundled:
        return bundled

    exe = shutil.which("ffmpeg")
    if not exe:
        raise ConversionError(
            "FFmpeg nao encontrado no PATH. Instale o FFmpeg e tente novamente.\n"
            "Windows: https://www.gyan.dev/ffmpeg/builds/ (versao 'full')."
        )
    return exe


# FPS maximo que um GIF toca na velocidade CORRETA. O formato GIF guarda o
# tempo de cada quadro em centesimos de segundo (inteiros); abaixo de 2cs
# (=50fps) os players tratam o delay como 10cs e o GIF fica em camera lenta.
GIF_MAX_FPS = 50

FORMATS = ("gif", "mp4", "webm")

# Motores de upscaling.
UPSCALE_ENGINES = ("classico", "ia")

# Motores de interpolacao de quadros (aumento de fps).
INTERP_ENGINES = ("ffmpeg", "rife")
RIFE_MODEL = "rife-v4.6"  # suporta contagem de quadros arbitraria (-n)

# Modelos do Real-ESRGAN. animevideov3 aceita escala 2/3/4 e e o melhor para
# GIFs/desenho/anime; os *-x4plus so fazem 4x.
REALESRGAN_MODELS = {
    "anime": "realesr-animevideov3",       # 2/3/4x - desenho/anime/GIF
    "foto": "realesrgan-x4plus",           # 4x - fotos reais
    "ilustracao": "realesrgan-x4plus-anime",  # 4x - ilustracao/anime
}
_MODELS_ONLY_4X = {"realesrgan-x4plus", "realesrgan-x4plus-anime"}


def find_realesrgan() -> tuple[str, str]:
    """Retorna (caminho_do_exe, caminho_da_pasta_models) do Real-ESRGAN.

    Levanta ConversionError se o binario embutido nao for encontrado.
    """
    exe_name = (
        "realesrgan-ncnn-vulkan.exe" if os.name == "nt" else "realesrgan-ncnn-vulkan"
    )
    exe = _find_bundled("realesrgan", exe_name)
    if not exe:
        raise ConversionError(
            "Real-ESRGAN nao encontrado. O upscaling por IA precisa do binario "
            "realesrgan-ncnn-vulkan (pasta 'realesrgan')."
        )
    models = _find_bundled("realesrgan", "models")
    if not models:
        raise ConversionError("Pasta de modelos do Real-ESRGAN nao encontrada.")
    return str(exe), str(models)


def find_rife() -> tuple[str, str]:
    """Retorna (caminho_do_exe, caminho_do_modelo) do RIFE.

    Levanta ConversionError se o binario/modelo embutido nao for encontrado.
    """
    exe_name = "rife-ncnn-vulkan.exe" if os.name == "nt" else "rife-ncnn-vulkan"
    exe = _find_bundled("rife", exe_name)
    if not exe:
        raise ConversionError(
            "RIFE nao encontrado. A interpolacao por IA precisa do binario "
            "rife-ncnn-vulkan (pasta 'rife')."
        )
    model = _find_bundled("rife", RIFE_MODEL)
    if not model:
        raise ConversionError(f"Modelo {RIFE_MODEL} do RIFE nao encontrado.")
    return str(exe), str(model)


def probe_duration(path) -> float:
    """Retorna a duracao do arquivo em segundos (0.0 se nao conseguir)."""
    ffmpeg = find_ffmpeg()
    p = subprocess.run(
        [ffmpeg, "-i", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = (p.stderr or "") + (p.stdout or "")
    i = out.find("Duration:")
    if i != -1:
        ts = out[i + 9:].split(",", 1)[0].strip()
        d = _parse_ts(ts)
        if d:
            return d
    return 0.0


def extract_frame(path, time_s: float, out_path, width: int | None = None) -> bool:
    """Extrai um unico quadro em `time_s` para `out_path` (PNG).

    Se `width` for dado, redimensiona para essa largura (altura proporcional).
    Retorna True se o arquivo foi gerado.
    """
    ffmpeg = find_ffmpeg()
    vf = f"scale={width}:-1" if width else "scale=iw:-1"
    subprocess.run(
        [ffmpeg, "-y", "-ss", f"{max(0.0, time_s)}", "-i", str(path),
         "-frames:v", "1", "-vf", vf, str(out_path)],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return Path(out_path).is_file()


def extract_thumb(path, time_s: float, out_path, cw: int, ch: int) -> bool:
    """Extrai uma miniatura recortada no tamanho exato cw x ch."""
    ffmpeg = find_ffmpeg()
    vf = (f"scale={cw}:{ch}:force_original_aspect_ratio=increase,"
          f"crop={cw}:{ch}")
    subprocess.run(
        [ffmpeg, "-y", "-ss", f"{max(0.0, time_s)}", "-i", str(path),
         "-frames:v", "1", "-vf", vf, str(out_path)],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return Path(out_path).is_file()


def _minterp(fps: int, quality: str) -> str:
    if quality == "alta":
        # mci = motion compensated interpolation (melhor qualidade)
        return (
            f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:"
            f"me_mode=bidir:vsbmc=1"
        )
    # blend = mistura simples entre quadros (mais rapido, sem artefatos)
    return f"minterpolate=fps={fps}:mi_mode=blend"


def _scale_chain(upscale: float, max_width: int | None = None) -> str:
    """
    Amplia a resolucao com Lanczos (alta qualidade) e realca a nitidez.

    Sempre forca dimensoes pares (trunc(.../2)*2), exigencia do yuv420p nos
    videos e inofensivo para o GIF. Com upscale=1.0 apenas normaliza as
    dimensoes, sem ampliar nem realcar.

    Se `max_width` for dado, reduz (nunca amplia) a largura final para no
    maximo esse valor, mantendo a proporcao e dimensoes pares.
    """
    scale = (
        f"scale=trunc(iw*{upscale}/2)*2:trunc(ih*{upscale}/2)*2:flags=lanczos"
    )
    if upscale > 1.0:
        # unsharp: realce moderado de nitidez apos a ampliacao.
        scale = f"{scale},unsharp=5:5:0.8:5:5:0.0"
    if max_width:
        # min(iw,MW): so reduz; -2 mantem proporcao e altura par; virgula
        # escapada para nao ser lida como separador de filtros.
        scale = (
            f"{scale},scale='min(iw\\,{max_width})':-2:flags=lanczos"
        )
    return scale


def _in_args(src, start: float = 0.0, duration=None) -> list[str]:
    """Argumentos de entrada do FFmpeg com corte opcional.

    -ss antes do -i (busca rapida) e -t depois (limita a duracao de saida).
    """
    args: list[str] = []
    if start and start > 0:
        args += ["-ss", f"{start}"]
    args += ["-i", str(src)]
    if duration and duration > 0:
        args += ["-t", f"{duration}"]
    return args


def build_filter(
    fps: int, quality: str, output_format: str = "gif", upscale: float = 1.0,
    max_width: int | None = None,
) -> str:
    """
    Monta a cadeia de filtros do FFmpeg.

    Ordem: interpolacao de movimento (em resolucao original, mais rapido) ->
    ampliacao Lanczos + nitidez -> limite de largura -> saida (paleta para
    GIF; dimensoes pares para video).
    """
    chain = f"{_minterp(fps, quality)},{_scale_chain(upscale, max_width)}"

    if output_format == "gif":
        # Gera paleta e aplica para preservar as cores no GIF final.
        return (
            f"[0:v]{chain},split[a][b];"
            f"[a]palettegen=stats_mode=diff[p];"
            f"[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
        )

    return f"[0:v]{chain}"


def _output_args(output_format: str) -> list[str]:
    """Argumentos de codec/saida especificos de cada formato."""
    if output_format == "gif":
        return ["-loop", "0"]  # mantem o loop infinito do GIF
    if output_format == "mp4":
        return [
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-crf", "18",
            "-movflags", "+faststart",
            "-an",
        ]
    if output_format == "webm":
        return [
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuv420p",
            "-crf", "24",
            "-b:v", "0",
            "-an",
        ]
    raise ConversionError(f"Formato de saida invalido: {output_format}")


def _run(cmd: list[str], tool: str, on_progress=None, band=None) -> list[str]:
    """Executa um comando, transmitindo a saida linha a linha.

    Se `band=(lo, hi)` for dado, tenta reportar progresso real do FFmpeg
    (via Duration/time=) mapeado nesse intervalo de porcentagem.

    Retorna as linhas de log; levanta ConversionError se o codigo != 0.
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # registra o processo para que a GUI possa mata-lo ao cancelar
    on_proc = _ACTIVE.get("on_process")
    if on_proc:
        on_proc(process)

    dur: float | None = None
    log_lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        if _cancelled():
            process.kill()
            break
        line = line.rstrip()
        log_lines.append(line)
        if band is not None:
            if dur is None and "Duration:" in line:
                i = line.find("Duration:") + 9
                dur = _parse_ts(line[i:].split(",", 1)[0].strip())
            j = line.find("time=")
            if j != -1 and dur and dur > 0:
                cur = _parse_ts(line[j + 5:].split(" ", 1)[0])
                if cur is not None:
                    frac = max(0.0, min(1.0, cur / dur))
                    _report(band[0] + frac * (band[1] - band[0]))
        if on_progress:
            on_progress(line)
    process.wait()

    if _cancelled():
        raise ConversionCancelled("Conversao cancelada.")
    if process.returncode != 0:
        tail = "\n".join(log_lines[-15:])
        raise ConversionError(f"{tool} falhou (codigo {process.returncode}):\n{tail}")
    return log_lines


def _run_with_poll(cmd, tool, watch_dir, expected, band, on_progress=None) -> None:
    """Executa `cmd` e, em paralelo, reporta progresso contando os PNGs
    gerados em `watch_dir` (0..expected) mapeados no intervalo `band`.

    Usado para RIFE/Real-ESRGAN, que geram os quadros progressivamente.
    """
    lo, hi = band
    stop = threading.Event()
    watch = Path(watch_dir)
    exp = max(1, expected)

    def poll():
        while not stop.is_set():
            try:
                n = sum(1 for _ in watch.glob("*.png"))
            except OSError:
                n = 0
            _report(lo + min(1.0, n / exp) * (hi - lo))
            stop.wait(0.25)

    th = threading.Thread(target=poll, daemon=True)
    th.start()
    try:
        _run(cmd, tool, on_progress)
    finally:
        stop.set()
        th.join(timeout=1)
    _report(hi)


def _parse_duration(log_lines: list[str]) -> float:
    """Extrai a duracao (segundos) do log do FFmpeg, pelo maior 'time='."""
    best = 0.0
    for line in log_lines:
        idx = line.find("time=")
        if idx == -1:
            continue
        stamp = line[idx + 5:].split(" ", 1)[0]
        try:
            h, m, s = stamp.split(":")
            secs = int(h) * 3600 + int(m) * 60 + float(s)
            best = max(best, secs)
        except (ValueError, IndexError):
            continue
    return best


def _encode_from_frames(
    ffmpeg: str, frames_glob: str, fps: int, output_format: str, dst: Path,
    upscale: float = 1.0, on_progress=None, band=None, max_width=None,
) -> None:
    """Monta o arquivo final a partir de uma sequencia de PNGs numerados.

    Aplica upscale classico (Lanczos+nitidez) quando upscale > 1.0 e limita a
    largura quando max_width e dado.
    """
    if band:
        _report(band[0])
    scale = _scale_chain(upscale, max_width)  # normaliza dimensoes/limita largura
    if output_format == "gif":
        vf = (
            f"{scale},split[a][b];[a]palettegen=stats_mode=diff[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
        )
        args = ["-filter_complex", vf, *_output_args("gif")]
    else:
        args = ["-vf", scale, *_output_args(output_format)]
    cmd = [
        ffmpeg, "-y",
        "-framerate", str(fps),
        "-i", frames_glob,
        *args,
        str(dst),
    ]
    _run(cmd, "FFmpeg (montagem)", on_progress)
    if band:
        _report(band[1])


def _interp_frames_ffmpeg(
    ffmpeg: str, src: Path, outdir: Path, fps: int, quality: str,
    on_progress=None, band=None, start=0.0, duration=None,
) -> int:
    """Interpola com FFmpeg (minterpolate) e extrai os quadros no fps alvo."""
    _run(
        [
            ffmpeg, "-y", *_in_args(src, start, duration),
            "-vf", _minterp(fps, quality),
            "-pix_fmt", "rgb24",  # sem alfa: RIFE/ESRGAN embaralham rgba
            str(outdir / "%08d.png"),
        ],
        "FFmpeg (interpolacao)", on_progress, band=band,
    )
    return len(list(outdir.glob("*.png")))


def _interp_frames_rife(
    ffmpeg: str, src: Path, outdir: Path, fps: int,
    on_progress=None, on_notice=None, band=None, start=0.0, duration=None,
) -> int:
    """Interpola com RIFE (IA): extrai os quadros originais e sintetiza os
    intermediarios ate atingir o fps alvo."""
    rife, model = find_rife()

    tmp = Path(tempfile.mkdtemp(prefix="rife_orig_"))
    try:
        # Extrai os quadros ORIGINAIS (sem duplicar), preservando a contagem.
        # rgb24 e obrigatorio: com alfa (rgba) o RIFE produz ruido.
        log = _run(
            [
                ffmpeg, "-y", *_in_args(src, start, duration),
                "-fps_mode", "passthrough",
                "-pix_fmt", "rgb24",
                str(tmp / "%08d.png"),
            ],
            "FFmpeg (extracao)", on_progress,
        )
        n_src = len(list(tmp.glob("*.png")))
        if n_src < 2:
            raise ConversionError(
                "RIFE precisa de pelo menos 2 quadros de origem."
            )
        duration = _parse_duration(log)
        if duration <= 0:
            duration = n_src / 10.0  # fallback conservador
        target = max(2, round(fps * duration))
        if on_notice:
            on_notice(
                f"RIFE: interpolando {n_src} -> {target} quadros (IA)... pode demorar."
            )
        rife_cmd = [
            rife, "-i", str(tmp), "-o", str(outdir),
            "-n", str(target), "-m", model,
        ]
        if band:
            _run_with_poll(rife_cmd, "RIFE", outdir, target, band, on_progress)
        else:
            _run(rife_cmd, "RIFE", on_progress)
        return len(list(outdir.glob("*.png")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _convert_pipeline(
    ffmpeg: str, src: Path, dst: Path, fps: int, quality: str,
    output_format: str, upscale: float, upscale_engine: str, ai_model: str,
    interp_engine: str, on_progress=None, on_notice=None,
    start=0.0, duration=None, max_width=None,
) -> None:
    """
    Pipeline baseado em quadros (usado quando ha RIFE e/ou upscale por IA):
      1. interpola os quadros ate o fps alvo (FFmpeg minterpolate ou RIFE);
      2. (opcional) amplia cada quadro com o Real-ESRGAN;
      3. remonta no formato final (upscale classico aplicado aqui, se for o caso).
    """
    # bandas de progresso (%) por etapa, conforme o que sera executado
    has_ia = upscale_engine == "ia" and upscale > 1.0
    if has_ia:
        b_interp, b_up, b_enc = (2, 45), (45, 92), (92, 100)
    else:
        b_interp, b_up, b_enc = (2, 90), None, (90, 100)

    workdir = Path(tempfile.mkdtemp(prefix="gifconv_"))
    frames = workdir / "frames"
    frames.mkdir()
    try:
        _report(0)
        # 1) interpolacao
        if on_notice:
            on_notice("Etapa 1: gerando os quadros interpolados...")
        if interp_engine == "rife":
            n = _interp_frames_rife(ffmpeg, src, frames, fps, on_progress,
                                    on_notice, band=b_interp,
                                    start=start, duration=duration)
        else:
            n = _interp_frames_ffmpeg(ffmpeg, src, frames, fps, quality,
                                      on_progress, band=b_interp,
                                      start=start, duration=duration)
        if n == 0:
            raise ConversionError("Nenhum quadro gerado na interpolacao.")
        _report(b_interp[1])

        cur_glob = str(frames / "%08d.png")
        classic_upscale = upscale

        # 2) upscaling por IA (opcional)
        if upscale_engine == "ia" and upscale > 1.0:
            esrgan, models_dir = find_realesrgan()
            model_name = REALESRGAN_MODELS.get(ai_model, ai_model)
            ai_scale = int(round(upscale))
            if model_name in _MODELS_ONLY_4X and ai_scale != 4:
                if on_notice:
                    on_notice(
                        f"O modelo '{ai_model}' so faz 4x; usando 4x. "
                        f"Para 2x/3x use o modelo 'anime'."
                    )
                ai_scale = 4
            upframes = workdir / "up"
            upframes.mkdir()
            if on_notice:
                on_notice(f"Etapa 2: IA ampliando {n} quadros ({ai_scale}x)... pode demorar.")
            esrgan_cmd = [
                esrgan, "-i", str(frames), "-o", str(upframes),
                "-n", model_name, "-s", str(ai_scale),
                "-m", models_dir, "-f", "png",
            ]
            _run_with_poll(esrgan_cmd, "Real-ESRGAN", upframes, n, b_up, on_progress)
            cur_glob = str(upframes / "%08d.png")
            classic_upscale = 1.0  # a IA ja ampliou; nao ampliar de novo

        # 3) remontagem (aplica upscale classico se ainda nao ampliou)
        if on_notice:
            on_notice("Etapa final: montando o arquivo...")
        _encode_from_frames(
            ffmpeg, cur_glob, fps, output_format, dst, classic_upscale,
            on_progress, band=b_enc, max_width=max_width
        )
        _report(100)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def convert_gif(
    input_path: str | os.PathLike,
    output_path: str | os.PathLike | None = None,
    fps: int = 60,
    quality: str = "alta",
    output_format: str = "gif",
    upscale: float = 1.0,
    upscale_engine: str = "classico",
    ai_model: str = "anime",
    interp_engine: str = "ffmpeg",
    start: float = 0.0,
    duration=None,
    max_width=None,
    on_progress=None,
    on_notice=None,
    cancel=None,
    on_process=None,
    on_percent=None,
) -> str:
    """
    Aumenta o FPS de `input_path` gerando quadros intermediarios.

    cancel: um threading.Event (ou objeto com .is_set()); quando acionado,
        o processo em andamento e encerrado e ConversionCancelled e levantada.
    on_process: callback chamado com cada subprocesso iniciado (para a GUI
        conseguir mata-lo ao cancelar).

    Args:
        input_path: caminho do arquivo de entrada (.gif recomendado; aceita
            mp4/webm tambem).
        output_path: caminho de saida. Se None, usa
            "<nome>_<fps>fps.<ext>" na mesma pasta.
        fps: quadros por segundo desejados (padrao 60).
        quality: "alta" (mci) ou "rapida" (blend) - so para o motor FFmpeg.
        output_format: "gif", "mp4" ou "webm".
        upscale: fator de ampliacao da resolucao (1.0 = sem ampliar).
        upscale_engine: "classico" (Lanczos+nitidez, FFmpeg) ou "ia"
            (Real-ESRGAN, reconstroi detalhes).
        ai_model: modelo do Real-ESRGAN ("anime", "foto", "ilustracao").
        interp_engine: motor de interpolacao de quadros: "ffmpeg"
            (minterpolate) ou "rife" (IA, melhor fluidez).
        on_progress: callback opcional que recebe cada linha de log.
        on_notice: callback opcional para avisos (ex.: teto de fps do GIF).

    Returns:
        O caminho do arquivo gerado.
    """
    if upscale < 1.0:
        raise ConversionError("O fator de upscale precisa ser >= 1.0")

    upscale_engine = upscale_engine.lower()
    if upscale_engine not in UPSCALE_ENGINES:
        raise ConversionError(f"Motor de upscale invalido: {upscale_engine}")

    interp_engine = interp_engine.lower()
    if interp_engine not in INTERP_ENGINES:
        raise ConversionError(f"Motor de interpolacao invalido: {interp_engine}")

    if upscale_engine == "ia" and upscale > 1.0 and int(round(upscale)) not in (2, 3, 4):
        raise ConversionError("O upscale por IA aceita apenas 2x, 3x ou 4x.")

    ffmpeg = find_ffmpeg()

    output_format = output_format.lower()
    if output_format not in FORMATS:
        raise ConversionError(f"Formato de saida invalido: {output_format}")

    src = Path(input_path)
    if not src.is_file():
        raise ConversionError(f"Arquivo nao encontrado: {src}")

    # GIF nao toca acima de ~50fps na velocidade certa (limitacao do formato).
    # Acima disso ficaria em camera lenta, entao limitamos e avisamos.
    if output_format == "gif" and fps > GIF_MAX_FPS:
        if on_notice:
            on_notice(
                f"GIF nao reproduz {fps}fps na velocidade correta; "
                f"limitado a {GIF_MAX_FPS}fps. Para {fps}fps de verdade, "
                f"use MP4 ou WebM."
            )
        fps = GIF_MAX_FPS

    if output_path is None:
        dst = src.with_name(f"{src.stem}_{fps}fps.{output_format}")
    else:
        dst = Path(output_path)

    # ativa o contexto de cancelamento/progresso (limpo no finally)
    _ACTIVE["cancel"] = cancel
    _ACTIVE["on_process"] = on_process
    _ACTIVE["on_percent"] = on_percent
    try:
        _report(0)
        # Pipeline baseado em quadros: interpolacao por RIFE OU upscale por IA.
        need_frames = interp_engine == "rife" or (upscale_engine == "ia" and upscale > 1.0)
        if need_frames:
            _convert_pipeline(
                ffmpeg, src, dst, fps, quality, output_format, upscale,
                upscale_engine, ai_model, interp_engine, on_progress, on_notice,
                start=start, duration=duration, max_width=max_width,
            )
        else:
            # --- caminho classico (uma unica passada no FFmpeg) ---
            filtergraph = build_filter(fps, quality, output_format, upscale, max_width)
            cmd = [
                ffmpeg,
                "-y",                      # sobrescreve saida
                *_in_args(src, start, duration),
                "-filter_complex", filtergraph,
                *_output_args(output_format),
                str(dst),
            ]
            _run(cmd, "FFmpeg", on_progress, band=(0, 100))
            _report(100)
    except ConversionCancelled:
        # remove saida parcial deixada por um processo interrompido
        try:
            if dst.exists():
                dst.unlink()
        except OSError:
            pass
        raise
    finally:
        _ACTIVE["cancel"] = None
        _ACTIVE["on_process"] = None
        _ACTIVE["on_percent"] = None

    if not dst.is_file():
        raise ConversionError("A conversao terminou mas o arquivo nao foi gerado.")
    return str(dst)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Converte um GIF para FPS maior usando interpolacao (FFmpeg)."
    )
    parser.add_argument("input", help="GIF de entrada")
    parser.add_argument("-o", "--output", help="Arquivo de saida")
    parser.add_argument("--fps", type=int, default=60, help="FPS alvo (padrao 60)")
    parser.add_argument(
        "--quality",
        choices=["alta", "rapida"],
        default="alta",
        help="alta = compensacao de movimento; rapida = mistura de quadros",
    )
    parser.add_argument(
        "-f", "--format",
        choices=list(FORMATS),
        default="mp4",
        help="Formato de saida: mp4/webm (60fps reais) ou gif (max 50fps)",
    )
    parser.add_argument(
        "-u", "--upscale",
        type=float,
        default=1.0,
        help="Fator de ampliacao da resolucao (ex.: 2 = dobra). Padrao 1 (sem).",
    )
    parser.add_argument(
        "-e", "--engine",
        choices=list(UPSCALE_ENGINES),
        default="classico",
        help="classico = Lanczos+nitidez (rapido); ia = Real-ESRGAN (melhor).",
    )
    parser.add_argument(
        "-m", "--model",
        choices=list(REALESRGAN_MODELS),
        default="anime",
        help="Modelo da IA: anime (2/3/4x), foto (4x), ilustracao (4x).",
    )
    parser.add_argument(
        "-I", "--interp",
        choices=list(INTERP_ENGINES),
        default="ffmpeg",
        help="Motor de interpolacao: ffmpeg (minterpolate) ou rife (IA).",
    )
    parser.add_argument(
        "--start", type=float, default=0.0,
        help="Segundo inicial do corte (padrao 0).",
    )
    parser.add_argument(
        "--duration", type=float, default=None,
        help="Duracao em segundos a converter (padrao: tudo).",
    )
    parser.add_argument(
        "--max-width", type=int, default=None, dest="max_width",
        help="Largura maxima do resultado em px (reduz se maior; mantem proporcao).",
    )
    args = parser.parse_args()

    extra = f", interp {args.interp}"
    if args.upscale > 1:
        extra += f", upscale {args.upscale}x ({args.engine})"
    print(f"Convertendo {args.input} para {args.fps} fps ({args.format}){extra}...")
    out = convert_gif(
        args.input,
        args.output,
        fps=args.fps,
        quality=args.quality,
        output_format=args.format,
        upscale=args.upscale,
        upscale_engine=args.engine,
        ai_model=args.model,
        interp_engine=args.interp,
        start=args.start,
        duration=args.duration,
        max_width=args.max_width,
        on_notice=lambda m: print(f"[info] {m}"),
        on_percent=lambda p: print(f"\r  progresso: {p:5.1f}%", end="", flush=True),
    )
    print(f"\nPronto! Arquivo gerado: {out}")
