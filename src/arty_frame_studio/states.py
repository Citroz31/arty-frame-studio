"""Liste d'états à mesurer : un mot, le niveau de TR et un nom par ligne.

Le format est un texte (ou un CSV) lisible dans un tableur : un état par ligne,
colonnes séparées par ``;``, ``,`` ou une tabulation, et seule la première est
obligatoire.

.. code-block:: text

    # mot ; TR ; nom
    000000000000000000000000 ; TX ; référence
    000000000000000000000001 ; RX
    0x00A5F3

* **mot** : binaire par défaut (zéros de tête compris), ``0x`` pour l'hexadécimal,
  ``0b`` pour forcer le binaire ;
* **TR** : ``TX``, ``RX``, ou le niveau de la broche (``1`` = 3,3 V, ``0`` = 0 V) ;
  vide : la broche TR n'est pas touchée par cet état ;
* **nom** : libre, repris dans les résultats.

Les lignes vides et ce qui suit ``#`` sont ignorés ; une première ligne d'en-tête
(``mot;TR;nom``) est reconnue et sautée. Au plus 4096 états (12 bits distincts).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from .sweep import SweepState, _token_value, check_width, word_text

MAX_STATES = 4096
MAX_FILE_BYTES = 2 * 1024 * 1024
_COLUMN_SEPARATORS = re.compile(r"[;,\t]")
_HEADERS = {"mot", "word", "mot_bin", "etat", "état", "state", "bits"}
_TR_TEXT = {"tx": True, "rx": False}


def _lines(text: str) -> list[tuple[int, list[str]]]:
    """Lignes utiles : (numéro, colonnes) sans commentaires ni en-tête."""
    rows: list[tuple[int, list[str]]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if _COLUMN_SEPARATORS.search(line):
            columns = [column.strip() for column in _COLUMN_SEPARATORS.split(line, maxsplit=2)]
        else:
            columns = line.split(None, 2)
        if not rows and columns[0].lower() in _HEADERS:
            continue
        rows.append((number, columns))
    return rows


def _tr_level(text: str, tx_level: int) -> int | None:
    value = text.strip().lower()
    if not value:
        return None
    if value in ("0", "1"):
        return int(value)
    if value in _TR_TEXT:
        return tx_level if _TR_TEXT[value] else 1 - tx_level
    raise ValueError(f"TR « {text} » : écrire TX, RX, 1 ou 0 (ou laisser vide).")


def infer_states_width(text: str, base: str = "bin") -> int | None:
    """Largeur commune si tous les mots sont binaires de même longueur, sinon ``None``."""
    widths = set()
    for _, columns in _lines(text):
        try:
            _, digits = _token_value(columns[0], base)
        except ValueError:
            return None
        if digits is None:
            return None
        widths.add(digits)
    return widths.pop() if len(widths) == 1 else None


def parse_states(
    text: str, bit_count: int, base: str = "bin", *, tx_level: int = 1
) -> list[SweepState]:
    """États du texte ; ``tx_level`` est le niveau de TR (1 = 3,3 V) qui signifie TX."""
    if base not in ("bin", "hex", "dec"):
        raise ValueError("Base attendue : bin, hex ou dec.")
    if tx_level not in (0, 1):
        raise ValueError("Niveau de TX : 0 ou 1.")
    check_width(bit_count)
    rows = _lines(text)
    if not rows:
        raise ValueError("Aucun état : saisir ou charger au moins un mot.")
    if len(rows) > MAX_STATES:
        raise ValueError(f"{len(rows)} états : {MAX_STATES} au maximum.")
    states = []
    for number, columns in rows:
        try:
            word, _ = _token_value(columns[0], base)
            if word >= 1 << bit_count:
                raise ValueError(f"« {columns[0]} » dépasse la largeur de {bit_count} bits.")
            tr = _tr_level(columns[1], tx_level) if len(columns) > 1 else None
            name = columns[2] if len(columns) > 2 else ""
            states.append(SweepState(word, tr, name))
        except ValueError as exc:
            raise ValueError(f"Ligne {number} : {exc}") from exc
    return states


def load_states_text(path: Path) -> str:
    """Contenu d'un fichier d'états (UTF-8, avec ou sans BOM de tableur)."""
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"Fichier trop volumineux (plus de {MAX_FILE_BYTES // 1024} Kio).")
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Le fichier d'états doit être en UTF-8.") from exc


def describe_states(states: Sequence[SweepState], bit_count: int, tx_level: int = 1) -> str:
    """« 64 états de 24 bits · 000… → 111… · TR : 32 TX, 32 RX » pour l'aperçu."""
    if not states:
        return "Aucun état."
    text = (
        f"{len(states)} état(s) de {bit_count} bits · "
        f"{word_text(states[0].word, bit_count)} → {word_text(states[-1].word, bit_count)}"
    )
    driven = [state for state in states if state.tr is not None]
    if driven:
        transmit = sum(1 for state in driven if state.tr == tx_level)
        text += f" · TR : {transmit} TX, {len(driven) - transmit} RX"
        if len(driven) != len(states):
            text += f", {len(states) - len(driven)} sans TR"
    return text
