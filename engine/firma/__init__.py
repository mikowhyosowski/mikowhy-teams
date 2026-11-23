"""Widok „Firma” — metamorfoza systemu na realny świat (osiedle biur, z góry).

Coś jest czymś:
  projekt   -> biuro (działka na osiedlu)
  milestone -> trofeum w gabinecie kierownika projektu
  agent/rola -> pokój (mniejszy kwadrat) w biurze
  zadanie   -> biurko w pokoju / teczka w strefie biura (wg statusu)
  event     -> dzwonek + przejazd teczki (animacja po stronie JS)
  host/LAN  -> recepcja przy bramie
  sys load  -> pogoda nad osiedlem
"""
from engine.firma.view import load_config, build_world  # noqa: F401
