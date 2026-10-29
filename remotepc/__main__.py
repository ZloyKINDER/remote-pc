"""Command line entry point for the host."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import logging
import ssl
import sys
from pathlib import Path

from aiohttp import web

from . import __version__, auth
from .config import HostConfig, default_config_path
from .h264 import find_ffmpeg
from .server import RemotePCServer, ServerSettings
from .tls import default_cert_dir, ensure_cert
from .winapi import enable_dpi_awareness, get_monitors, local_ipv4_addresses

log = logging.getLogger("remotepc")


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _bounded_int(low: int, high: int):
    def parse(value: str) -> int:
        number = int(value)
        if not (low <= number <= high):
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return number

    return parse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remotepc",
        description="RemotePC — простое управление компьютером через браузер по IP.",
    )
    parser.add_argument("--port", type=_bounded_int(1, 65535), default=8765, help="TCP-порт (по умолчанию 8765)")
    parser.add_argument("--bind", default="0.0.0.0", help="адрес прослушивания (по умолчанию 0.0.0.0)")
    parser.add_argument("--password", help="пароль подключения; без флага будет запрошен")
    parser.add_argument("--public-ip", help="ваш белый IP (для сертификата и подсказки)")
    parser.add_argument(
        "--max-width",
        type=_bounded_int(640, 7680),
        default=3840,
        help="жёсткий предел ширины кадра (по умолчанию 3840)",
    )
    parser.add_argument("--fps", type=_bounded_int(5, 60), default=60, help="лимит кадров/с: 15/30/45/60 (по умолчанию 60)")
    parser.add_argument(
        "--quality",
        type=_bounded_int(30, 95),
        default=85,
        help="стартовое качество изображения (по умолчанию 85)",
    )
    parser.add_argument("--cert-dir", type=Path, help="каталог для самоподписанного сертификата")
    parser.add_argument("--no-tls", action="store_true", help="работать без HTTPS (не рекомендуется)")
    parser.add_argument("--demo", action="store_true", help="тестовый экран вместо рабочего стола")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument(
        "--config",
        type=Path,
        help=f"JSON-конфиг (по умолчанию {default_config_path()})",
    )
    parser.add_argument("--gui", action="store_true", help="открыть графический интерфейс хоста")
    parser.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--install-admin-task", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--remove-admin-task", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--status", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"RemotePC {__version__}")
    return parser


def _choose_password(cli_value: str | None) -> tuple[str, bool]:
    """Return (password, generated)."""

    if cli_value is not None:
        if not (6 <= len(cli_value) <= 256):
            raise SystemExit("Пароль должен быть длиной от 6 до 256 символов.")
        return cli_value, False

    if not sys.stdin.isatty():
        return auth.generate_password(), True

    print("Задайте пароль для подключения.")
    print("Минимум 6 символов. Нажмите Enter, чтобы сгенерировать надёжный пароль.")
    for _ in range(4):
        first = getpass.getpass("Пароль: ")
        if not first:
            return auth.generate_password(), True
        if len(first) < 6:
            print("Слишком короткий пароль (нужно минимум 6 символов).")
            continue
        second = getpass.getpass("Повторите пароль: ")
        if first != second:
            print("Пароли не совпали, попробуйте ещё раз.")
            continue
        return first, False
    raise SystemExit("Не удалось задать пароль.")


def _print_banner(
    port: int,
    password: str,
    generated: bool,
    tls: bool,
    fingerprint: str | None,
    public_ip: str | None,
    bind: str,
) -> None:
    scheme = "https" if tls else "http"
    line = "=" * 68
    print(line)
    print("  RemotePC — хост запущен")
    print(line)
    if public_ip:
        host = f"[{public_ip}]" if ":" in public_ip else public_ip
        print(f"  Из интернета:   {scheme}://{host}:{port}")
    wildcard = bind in ("0.0.0.0", "::", "")
    addresses = local_ipv4_addresses() if wildcard else [bind]
    if addresses:
        print("  В локальной сети:")
        for index, address in enumerate(addresses):
            suffix = "  (основной)" if index == 0 else ""
            print(f"                  {scheme}://{address}:{port}{suffix}")
    if not addresses and not public_ip:
        print(f"  Адрес:          {scheme}://<IP-этого-ПК>:{port}")
    print()
    if generated:
        print(f"  Пароль:         {password}")
    else:
        print("  Пароль:         задан вами")
    if tls and fingerprint:
        print(f"  Сертификат:     {fingerprint}")
        print("  В браузере один раз подтвердите самоподписанный сертификат.")
    elif not tls:
        print("  ВНИМАНИЕ: TLS выключен, трафик и экран передаются открыто!")
    print()
    print(f"  Проброс порта в роутере: TCP {port} -> этот ПК.")
    print("  Если не подключается — разрешите порт в брандмауэре Windows.")
    print("  Ctrl+C — остановить хост.")
    print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.platform != "win32":
        print("RemotePC host works only on Windows.", file=sys.stderr)
        return 2

    if args.install_admin_task or args.remove_admin_task:
        from .admin_task import install_task, remove_task, write_status

        try:
            if args.install_admin_task:
                install_task(args.config)
            else:
                remove_task()
        except Exception as exc:
            write_status(args.status, False, str(exc))
            return 1
        write_status(args.status, True)
        return 0

    if args.autostart:
        args.gui = True
    if args.gui:
        from .gui import run_gui

        return run_gui(args.config, autostart=args.autostart)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("dxcam").setLevel(logging.WARNING)

    config_file = args.config
    if config_file is None and default_config_path().is_file():
        config_file = default_config_path()
    if config_file is not None:
        cfg = HostConfig.load(config_file)
        if args.password is None:
            args.password = cfg.password
        if args.port == 8765:
            args.port = cfg.port
        if args.bind == "0.0.0.0":
            args.bind = cfg.bind
        if args.max_width == 3840:
            args.max_width = cfg.max_width
        if args.fps == 60:
            args.fps = cfg.fps
        if args.quality == 85:
            args.quality = cfg.quality
        if args.public_ip is None:
            args.public_ip = cfg.public_ip
        if args.cert_dir is None and cfg.cert_dir:
            args.cert_dir = Path(cfg.cert_dir)
        args.no_tls = args.no_tls or not cfg.tls
        args.demo = args.demo or cfg.demo

    public_ip = args.public_ip
    if public_ip:
        try:
            public_ip = str(ipaddress.ip_address(public_ip.strip()))
        except ValueError:
            print("--public-ip должен быть корректным IP-адресом.", file=sys.stderr)
            return 2

    enable_dpi_awareness()
    monitors = get_monitors()
    if not monitors:
        print("Не удалось найти мониторы.", file=sys.stderr)
        return 2

    password, generated = _choose_password(args.password)
    default_codec = "h264" if (not args.demo and find_ffmpeg()) else "jpeg"
    settings = ServerSettings(
        fps=args.fps,
        max_width=args.max_width,
        quality=args.quality,
        codec=default_codec,
        demo=args.demo,
        tls=not args.no_tls,
    )

    ssl_context = None
    fingerprint = None
    if settings.tls:
        cert_dir = args.cert_dir or default_cert_dir()
        try:
            cert_path, key_path, fingerprint = ensure_cert(
                cert_dir,
                [public_ip] if public_ip else [],
            )
        except OSError as exc:
            if args.cert_dir:
                print(f"Не удалось подготовить TLS-сертификат в {cert_dir}: {exc}", file=sys.stderr)
                return 2
            fallback = Path.cwd() / ".certs"
            print(f"Не удалось записать сертификат в {cert_dir} ({exc}).")
            print(f"Использую {fallback}.")
            try:
                cert_path, key_path, fingerprint = ensure_cert(
                    fallback,
                    [public_ip] if public_ip else [],
                )
            except Exception as fallback_exc:
                print(f"Не удалось подготовить TLS-сертификат: {fallback_exc}", file=sys.stderr)
                return 2
        except Exception as exc:
            print(f"Не удалось подготовить TLS-сертификат: {exc}", file=sys.stderr)
            return 2
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.load_cert_chain(cert_path, key_path)

    server = RemotePCServer(password, monitors, settings)
    app = server.make_app()

    _print_banner(
        args.port,
        password,
        generated,
        settings.tls,
        fingerprint,
        public_ip,
        args.bind,
    )
    if args.demo:
        print("  ДЕМО-РЕЖИМ: передаётся тестовая картинка, не рабочий стол.")
        print("=" * 68, flush=True)

    try:
        web.run_app(
            app,
            host=args.bind,
            port=args.port,
            ssl_context=ssl_context,
            access_log=None,
            shutdown_timeout=3.0,
            print=lambda *a, **k: None,
        )
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"Не удалось запустить сервер на {args.bind}:{args.port}: {exc}", file=sys.stderr)
        return 1
    print("RemotePC остановлен.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
