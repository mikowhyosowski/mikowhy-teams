#!/usr/bin/env python3
"""Walidator Conventional Commits (CHAT-0006c).

Wymusza format naglowka: type(scope): opis, caly naglowek max N znakow
(domyslnie 72; konfiguracja przez zmienna srodowiskowa COMMIT_MSG_MAX_LEN,
brak hardkodu limitu w logice).

Wejscie (jeden z):
  commit_msg_check.py <plik-z-wiadomoscia>   (np. .git/COMMIT_EDITMSG)
  commit_msg_check.py --range <zakres-git>    (subjecty commitow z zakresu)

Wyjscie: kod 0 = OK, kod 1 = blokada z jawnym komunikatem bledu na stderr.
Logger zamiast print; pelne typowanie.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from typing import Optional

logger = logging.getLogger("commit_msg_check")

DEFAULT_MAX_LEN = 72
ENV_MAX_LEN = "COMMIT_MSG_MAX_LEN"


def get_max_len(env: Optional[dict[str, str]] = None) -> int:
    """Limit znakow naglowka z env COMMIT_MSG_MAX_LEN; fallback do domyslnego."""
    source = env if env is not None else os.environ
    raw = source.get(ENV_MAX_LEN, str(DEFAULT_MAX_LEN))
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r nie jest liczba calkowita; uzywam %d", ENV_MAX_LEN, raw, DEFAULT_MAX_LEN)
        return DEFAULT_MAX_LEN
    if value <= 0:
        logger.warning("%s=%d niedodatni; uzywam %d", ENV_MAX_LEN, value, DEFAULT_MAX_LEN)
        return DEFAULT_MAX_LEN
    return value


def header_pattern(max_len: int) -> re.Pattern[str]:
    """Regex Conventional Commits: type(opcjonalny scope): opis."""
    return re.compile(r"^[a-z]+(\([a-z0-9-]+\))?: .{1," + str(max_len) + r"}$")


def validate_message(message: str, max_len: Optional[int] = None) -> list[str]:
    """Waliduje jedna wiadomosc commita; zwraca liste bledow (pusta = OK).

    Walidowany jest tylko naglowek (pierwsza linia) — body commita jest
    dowolne wg specyfikacji Conventional Commits (edge-case wieloliniowy).
    """
    limit = get_max_len() if max_len is None else max_len
    lines = message.splitlines()
    header = lines[0] if lines else ""
    errors: list[str] = []
    if not header.strip():
        errors.append("pusty naglowek commita")
        return errors
    if len(header) > limit:
        errors.append("naglowek ma %d znakow, limit to %d: %r" % (len(header), limit, header))
    if not header_pattern(limit).match(header):
        errors.append(
            "niepoprawny format naglowka %r; oczekiwano 'type(scope): opis' "
            "(type=[a-z]+, scope=[a-z0-9-]+ opcjonalny, opis 1-%d znakow)" % (header, limit)
        )
    return errors


def validate_file(path: str, max_len: Optional[int] = None) -> list[str]:
    """Waliduje wiadomosc z pliku (np. COMMIT_EDITMSG)."""
    with open(path, "r", encoding="utf-8") as fh:
        return validate_message(fh.read(), max_len)


def validate_range(rev_range: str, max_len: Optional[int] = None) -> list[str]:
    """Waliduje subjecty wszystkich commitow z zakresu git (np. staging..feature/x).

    Osobny podzial na git log + walidacje, zeby komunikat bledu wskazywal
    dokladnie ktory commit łamie kontrakt przy merge do staging.
    """
    result = subprocess.run(
        ["git", "log", "--pretty=format:%s", rev_range],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return ["git log dla zakresu %r zwrocil kod %d: %s" % (rev_range, result.returncode, result.stderr.strip())]
    errors: list[str] = []
    subjects = [line for line in result.stdout.splitlines() if line.strip()]
    if not subjects:
        errors.append("zakres %r nie zawiera zadnych commitow" % rev_range)
    for subject in subjects:
        errors.extend(validate_message(subject, max_len))
    return errors


def main(argv: list[str]) -> int:
    """Punkt wejscia CLI; kod wyjscia 0/1. Dispatch w jednym miejscu, zeby
    hook merge do staging mial jeden stabilny interfejs (plik lub --range)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    if len(argv) < 2:
        logger.error("uzycie: %s <plik-z-wiadomoscia> | --range <zakres-git>", argv[0])
        return 1
    if argv[1] == "--range":
        if len(argv) < 3:
            logger.error("brak zakresu po --range")
            return 1
        errors = validate_range(argv[2])
    else:
        try:
            errors = validate_file(argv[1])
        except OSError as exc:
            logger.error("nie mozna odczytac pliku %r: %s", argv[1], exc)
            return 1
    if errors:
        for err in errors:
            logger.error("BLOKADA: %s", err)
        return 1
    logger.info("OK: komunikaty spelniaja Conventional Commits")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
