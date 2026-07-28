"""
Вход личного Telegram-аккаунта (нужен номер телефона).
Юзернейм (@name) для входа НЕ подходит — только phone + код.

Запуск: login.bat
"""

from __future__ import annotations

import os
import re
import sys
import traceback
from pathlib import Path

# sync-обёртка: иначе connect/send_code_request отдают coroutine и скрипт падает
from telethon.sync import TelegramClient
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberFloodError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
SESSION = ROOT / "data" / "user"
LOG = ROOT / "data" / "last_login_error.txt"


def fix_console() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


def load_env() -> dict[str, str]:
    data: dict[str, str] = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
            os.environ.setdefault(k.strip(), data[k.strip()])
    return data


def upsert_env(updates: dict[str, str]) -> None:
    existing: dict[str, str] = {}
    order: list[str] = []
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.strip().startswith("#") or "=" not in line:
                order.append(line)
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            existing[k] = v.strip()
            order.append(f"__KEY__{k}")
    for k, v in updates.items():
        if f"__KEY__{k}" not in order and k not in existing:
            order.append(f"__KEY__{k}")
        existing[k] = v
    lines: list[str] = []
    seen: set[str] = set()
    for item in order:
        if item.startswith("__KEY__"):
            k = item[7:]
            if k in seen:
                continue
            seen.add(k)
            lines.append(f"{k}={existing[k]}")
        else:
            lines.append(item)
    for k, v in existing.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    ENV.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def clear_session() -> None:
    for p in SESSION.parent.glob(SESSION.name + ".*"):
        try:
            p.unlink()
            print(f"  udalen: {p.name}")
        except OSError as e:
            print(f"  ne udalos udalit {p.name}: {e}")


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        raise SystemExit("\nVvod prervan. Zapustite login.bat eshche raz.")


def normalize_phone(raw: str) -> str:
    s = raw.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    # @username — нельзя
    if s.startswith("@"):
        raise SystemExit(
            "\n[X] Username (@name) dlya vhoda NE podhodit.\n"
            "Nuzhen NOMER TELEFONA, na kotoriy zaregistrirovan Telegram.\n"
            "Primer: +79001234567"
        )
    if re.fullmatch(r"8\d{10}", s):
        s = "+7" + s[1:]
    if s.isdigit() and not s.startswith("+"):
        s = "+" + s
    return s


def looks_like_bot_token(s: str) -> bool:
    return bool(re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{20,}", s.strip()))


def write_error(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        LOG.write_text(msg, encoding="utf-8")
    except OSError:
        pass


def describe_code_type(sent) -> tuple[str, str]:
    name = type(sent.type).__name__
    # SentCodeTypeApp — код в приложении
    if "App" in name:
        return (
            "PRILOZHENIE Telegram (ne SMS)",
            (
                "  1) Otkroyte Telegram na TELEFONE (tot zhe nomer).\n"
                "  2) Kod — VSPLYVAYUSHCHEE okno 'Kod dlya vhoda'.\n"
                "     Eto NE soobshchenie v chate i ne ot bota.\n"
                "  3) Esli otkryt Telegram Desktop/Web — kod mozhet byt tam.\n"
                "  4) Neskolko akkauntov — pereklyuchites na nuzhniy."
            ),
        )
    if "Sms" in name or "Firebase" in name or "Fragment" in name:
        return ("SMS na telefon", "  Proverte SMS (i papku Spam).")
    if "Call" in name or "Missed" in name:
        return (
            "ZVONOK na telefon",
            "  Pozvonyat / sbrosyat. Kod — cifry nomera ili golosom.",
        )
    if "Email" in name:
        return (
            "Nuzhna pochta",
            "  V Telegram nuzhno privyazat email, potom povtorite login.",
        )
    return (name, f"  Tip dostavki: {name}. Smotrite Telegram i SMS.")


def print_where_code_went(sent) -> None:
    where, how = describe_code_type(sent)
    print()
    print("=" * 52)
    print(f"  Kuda ushel kod: {where}")
    print("=" * 52)
    print(how)
    print()
    print("  [!] Kod pochti vsegda — OKNO v Telegram, ne chat.")
    print()
    timeout = getattr(sent, "timeout", None)
    if timeout:
        print(f"  Povtor ne ranshe chem cherez ~{timeout} sek.")
        print()


def main() -> None:
    fix_console()
    print("=" * 52)
    print("  DublePost — vhod LICHNOGO akkaunta")
    print("=" * 52)
    print()
    print("  Nuzhen NOMER TELEFONA (+7900...).")
    print("  Username (@name) — NELZYA, Telegram tak ne puskaet.")
    print("  Kod pridet V PRILOZHENIE (vsplyvashka), redko SMS.")
    print()

    env = load_env()
    api_id = (env.get("API_ID") or "").strip()
    api_hash = (env.get("API_HASH") or "").strip()

    if not api_id:
        api_id = ask("API_ID (chislo s my.telegram.org): ")
    if not api_hash:
        api_hash = ask("API_HASH (stroka s my.telegram.org): ")

    if not api_id.isdigit():
        raise SystemExit("API_ID dolzhen byt chislom. https://my.telegram.org")
    if len(api_hash) < 20:
        raise SystemExit("API_HASH slishkom korotkiy.")

    print(f"  API_ID = {api_id}")
    print()

    (ROOT / "data").mkdir(parents=True, exist_ok=True)

    if any(SESSION.parent.glob(SESSION.name + ".*")):
        print("Chishchu staruyu sessiyu...")
        clear_session()
        print()

    print("Primer: +79001234567")
    phone = normalize_phone(ask("Telefon: "))

    if not phone or looks_like_bot_token(phone):
        raise SystemExit(
            "\n[X] Eto token bota ili pusto.\nNuzhen nomer: +79001234567"
        )
    if not re.fullmatch(r"\+\d{10,15}", phone):
        raise SystemExit(
            f"\n[X] Neponyatniy nomer: {phone}\n"
            "Format: +79001234567\n"
            "Username (@xxx) — nelzya."
        )

    client = TelegramClient(str(SESSION), int(api_id), api_hash)

    try:
        print("Podklyuchayus k Telegram...")
        client.connect()
    except ApiIdInvalidError:
        write_error("ApiIdInvalidError")
        raise SystemExit(
            "\n[X] API_ID / API_HASH nevernye.\n"
            "https://my.telegram.org -> obnovite .env"
        )
    except Exception as e:
        write_error(traceback.format_exc())
        raise SystemExit(f"\n[X] Ne podklyuchilis: {type(e).__name__}: {e}")

    try:
        print(f"Zaprashivayu kod dlya {phone} ...")
        try:
            sent = client.send_code_request(phone)
        except PhoneNumberInvalidError:
            write_error("PhoneNumberInvalidError")
            clear_session()
            raise SystemExit(f"\n[X] Telegram ne prinyal nomer: {phone}")
        except PhoneNumberBannedError:
            write_error("PhoneNumberBannedError")
            clear_session()
            raise SystemExit("\n[X] Nomer zablokirovan v Telegram.")
        except PhoneNumberFloodError:
            write_error("PhoneNumberFloodError")
            clear_session()
            raise SystemExit(
                "\n[X] Slishkom mnogo zaprosov koda.\n"
                "Podozhdite neskolko chasov."
            )
        except FloodWaitError as e:
            write_error(f"FloodWait {e.seconds}")
            clear_session()
            mins = max(1, e.seconds // 60)
            raise SystemExit(
                f"\n[X] Telegram prosit podozhdat {e.seconds} sek (~{mins} min)."
            )
        except Exception as e:
            write_error(traceback.format_exc())
            clear_session()
            raise SystemExit(f"\n[X] Ne otpravili kod: {type(e).__name__}: {e}")

        # safety: never process a raw coroutine
        if hasattr(sent, "__await__"):
            write_error("send_code_request returned coroutine (sync broken)")
            clear_session()
            raise SystemExit(
                "\n[X] Vnutrennyaya oshibka Telethon sync.\n"
                "Napisite razrabotchiku / perezapustite login.bat"
            )

        print_where_code_went(sent)

        while True:
            print("Varianty:")
            print("  * vvedite KOD i Enter")
            print("  * r  — zaprosit kod eshche raz")
            print("  * q  — vyhod")
            code = ask("Kod (ili r/q): ").replace(" ", "")

            if not code or code.lower() == "q":
                clear_session()
                raise SystemExit("Vyhod. Sessiya sbrosena.")

            if code.lower() == "r":
                print("Povtornyy zapros koda...")
                try:
                    sent = client.send_code_request(phone)
                    print_where_code_went(sent)
                except FloodWaitError as e:
                    print(f"  Podozhdite {e.seconds} sek.")
                except Exception as e:
                    print(f"  Ne vyshlo: {type(e).__name__}: {e}")
                continue

            try:
                client.sign_in(
                    phone=phone, code=code, phone_code_hash=sent.phone_code_hash
                )
                break
            except SessionPasswordNeededError:
                print()
                print("Vklyuchen oblachnyy parol (2FA).")
                password = ask("Parol 2FA: ")
                try:
                    client.sign_in(password=password)
                    break
                except PasswordHashInvalidError:
                    write_error("PasswordHashInvalidError")
                    clear_session()
                    raise SystemExit("\n[X] Nevernyy parol 2FA.")
            except PhoneCodeInvalidError:
                print("  [X] Nevernyy kod. Eshche raz (iz VSPLYVASHKI Telegram).")
                continue
            except PhoneCodeExpiredError:
                print("  [X] Kod ustarel. Nazhmite r.")
                continue
            except FloodWaitError as e:
                write_error(f"FloodWait {e.seconds}")
                clear_session()
                raise SystemExit(f"\n[X] Flood wait {e.seconds} sek.")
            except Exception as e:
                write_error(traceback.format_exc())
                clear_session()
                raise SystemExit(f"\n[X] Oshibka vhoda: {type(e).__name__}: {e}")

        if not client.is_user_authorized():
            write_error("not authorized after sign_in")
            clear_session()
            raise SystemExit("\n[X] Vhod ne zavershilsya.")

        me = client.get_me()
        if getattr(me, "bot", False):
            client.disconnect()
            clear_session()
            raise SystemExit("\n[X] Voshli kak BOT. Nuzhen lichnyy nomer.")

        name = " ".join(
            x for x in [me.first_name, me.last_name or ""] if x
        ).strip() or me.username or str(me.id)

        print()
        print("=" * 52)
        print(f"  [OK] GOTOVO: {name} (id={me.id})")
        if me.username:
            print(f"     @{me.username}")
        print(r"     Sessiya: data\user.session")
        print("=" * 52)
        print()
        print("  Dalshe: start.bat")
        print("  V Telegram: @dublepostbot -> /start")
        print()

        upsert_env({"API_ID": str(api_id), "API_HASH": api_hash})
        if LOG.exists():
            try:
                LOG.unlink()
            except OSError:
                pass

    finally:
        try:
            client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    fix_console()
    try:
        main()
    except SystemExit as e:
        code = e.code
        if code not in (0, None):
            msg = str(code) if not isinstance(code, int) else ""
            if msg and msg != "0":
                try:
                    print(msg)
                except Exception:
                    pass
                write_error(msg)
        # keep exit code for bat
        if isinstance(code, int):
            raise
        raise SystemExit(1 if code else 0)
    except Exception:
        tb = traceback.format_exc()
        write_error(tb)
        print("\n[X] Neozhidannaya oshibka:")
        traceback.print_exc()
        print(f"\nPodrobnosti: {LOG}")
        raise SystemExit(1)
