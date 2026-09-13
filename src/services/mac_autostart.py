"""Best-effort LaunchAgent self-install for the frozen macOS .app.

The .dmg ships no installer script (unlike the curl|bash path or the Windows
Inno setup), so the app registers its own autostart on launch: write
``~/Library/LaunchAgents/com.goodpesik.barhandler-manager.plist`` pointing at
our own executable.

ЧУЖОГО АВТОЗАПУСКУ НЕ ЗАЙМАЄМО. Той самий файл і той самий лейбл пише
``installers/install.sh`` — скриптова інсталяція в ``~/.barhandler-manager``.
Обидві версії слухають той самий порт 9999, тож автозапуск має бути ОДИН. Хто
його тримає, вирішує той, кого поставили: якщо plist уже вказує на скриптову
інсталяцію, ми його НЕ перезаписуємо — інакше одне відкриття .dmg «на пробу»
мовчки підміняло б людині те, що стартує при вході в систему (знайдено ревʼю).
Замість цього пишемо в лог, що автозапуск лишився за скриптовою інсталяцією.

We only WRITE the plist here — we do NOT ``launchctl load`` it now, because
this process already holds port 9999 and a loaded agent would spawn a second
instance that can't bind (KeepAlive would then crash-loop it). launchd starts
it at the next login instead; the double-clicked session serves until then.
This mirrors the Windows install, where autostart also takes effect at logon.
"""

from __future__ import annotations

import logging
import plistlib
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_LABEL = "com.goodpesik.barhandler-manager"


_BUNDLED_AGENT = "com.goodpesik.barhandler-manager.plist"


def _register_via_service_management() -> bool:
    """Зареєструвати агент із бандла через SMAppService. True, якщо вийшло.

    Це той шлях, який дає системі впізнати НАС: у «Автозапуск і розширення»
    зʼявляється «Device Handler» з іконкою застосунку. Доти там стояло «ПЗ
    Maksym Levynets» — назва команди з сертифіката, бо plist у
    ~/Library/LaunchAgents система ні з яким бандлом не звʼязує.

    Працює лише для підписаного застосунку, який лежить у /Applications і має
    plist у Contents/Library/LaunchAgents. Будь-яка невдача — не помилка: нижче
    лишається старий спосіб, який працює завжди, тільки з чужою назвою.
    """
    bundle = Path(sys.executable).resolve().parent.parent.parent  # .../X.app
    if not (bundle / "Contents" / "Library" / "LaunchAgents" / _BUNDLED_AGENT).exists():
        logger.info("mac autostart: агента в бандлі немає — старий спосіб")
        return False
    try:
        from ServiceManagement import SMAppService  # type: ignore
    except Exception as exc:  # noqa: BLE001 — немає pyobjc у цій збірці
        logger.info("mac autostart: ServiceManagement недоступний (%s)", exc)
        return False
    try:
        service = SMAppService.agentServiceWithPlistName_(_BUNDLED_AGENT)
        # status 1 = enabled: уже зареєстровано, повторна реєстрація зайва.
        if int(service.status()) == 1:
            logger.info("mac autostart: агент уже зареєстрований системою")
            return True
        ok, err = service.registerAndReturnError_(None)
        if ok:
            logger.info("mac autostart: зареєстровано через SMAppService")
            return True
        logger.info("mac autostart: SMAppService відмовив (%s) — старий спосіб", err)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.info("mac autostart: SMAppService не спрацював (%s) — старий спосіб", exc)
        return False


def ensure_launch_agent() -> None:
    """Register a login LaunchAgent for the frozen mac app. No-op elsewhere.

    Idempotent: rewrites the plist only when it's missing or points at a
    different executable (so moving / reinstalling the .app re-targets it).
    Never raises — autostart registration must not block the server starting.
    """
    if not (getattr(sys, "frozen", False) and sys.platform == "darwin"):
        return

    # Спершу пробуємо системний шлях — він єдиний, що дає правильну назву в
    # «Автозапуск і розширення». Не вийшло — лишається старий plist у домівці.
    if _register_via_service_management():
        return

    try:
        exe = str(Path(sys.executable).resolve())
        log_dir = Path.home() / ".barhandler-manager"
        agents = Path.home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        plist_path = agents / f"{_LABEL}.plist"

        if plist_path.exists():
            try:
                current = plistlib.loads(plist_path.read_bytes())
                args = current.get("ProgramArguments") or []
                if args == [exe]:
                    return  # already registered for this exact app
                # Скриптова інсталяція запускає інтерпретатор із main.py — своє
                # ми пізнаємо за одним аргументом-бінарником. Усе інше вважаємо
                # чужим і не торкаємось.
                if len(args) > 1 or (args and args[0] != exe and args[0].endswith("python")):
                    logger.info(
                        "mac autostart НЕ змінено: %s уже веде на іншу інсталяцію (%s). "
                        "Ця копія працює, поки відкрита; автозапуск лишається за тією.",
                        plist_path, args,
                    )
                    return
            except Exception:
                pass  # unreadable/legacy → overwrite below

        plist_path.write_bytes(
            plistlib.dumps(
                {
                    "Label": _LABEL,
                    "ProgramArguments": [exe],
                    "RunAtLoad": True,
                    "KeepAlive": True,
                    "StandardOutPath": str(log_dir / "bhm.out.log"),
                    "StandardErrorPath": str(log_dir / "bhm.err.log"),
                }
            )
        )
        logger.info("mac autostart registered: %s -> %s", plist_path, exe)
    except Exception as exc:  # noqa: BLE001 — never block startup on this
        logger.warning("mac autostart self-install failed: %s", exc)
