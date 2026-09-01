# -*- coding: utf-8 -*-
"""
Garde anti-SSRF des URLs Graph Memory — couche ``core`` (ADR-0006/0010).

Cette fonction valide une URL de Graph Memory AVANT toute connexion réseau.
Elle est volontairement placée dans ``core/`` (et non dans ``tools/``) pour
deux raisons structurelles :

- ``core/`` ne doit JAMAIS importer ``tools/`` (back-edge interdit, ADR-0006 /
  ADR-0010). Or la couche adaptateur (``core/graph_bridge.py``) a désormais
  besoin de la même garde SSRF avant de construire un client à partir de la
  config stockée. Dupliquer le validateur risquerait une dérive ; on en fait
  donc une source unique dans ``core/``.
- ``tools/graph.py`` continue d'exposer ``_validate_gm_url`` en le ré-exportant
  d'ici, donc le nom et le comportement de la couche outil restent intacts.

Stdlib uniquement (``ipaddress`` + ``urllib.parse``) — aucune dépendance, aucune
construction de client à l'import.

Voir l'historique : LM2-02 fix (validation anti-SSRF du paramètre ``url`` de
graph_connect). Sans cette validation, n'importe quel token ``write`` pouvait
faire émettre une requête HTTP depuis le pod live-mem vers une URL arbitraire
(IP privée, metadata cloud 169.254.169.254, ...). L'URL persiste ensuite dans
``_meta.json`` et est ré-utilisée à chaque graph_push.
"""

import concurrent.futures
import ipaddress
import logging
import socket
from typing import Optional
from urllib.parse import urlparse

_logger = logging.getLogger("live_mem.url_guard")

_ALLOWED_GM_SCHEMES = ("http", "https")

# HM-11 (revue) : borne la résolution DNS. validate_gm_url est synchrone et
# appelée depuis des contextes async ; un DNS lent (potentiellement contrôlé par
# l'appelant via le hostname) ne doit pas geler l'event-loop indéfiniment. On
# borne à 2s ; au-delà on retombe sur « accepté » (fallback, comme gaierror) et
# la connexion réelle échouera de toute façon. NB : à terme, résoudre en amont
# via loop.getaddrinfo côté appelant async serait plus propre (pas de stall).
_DNS_RESOLVE_TIMEOUT_SECONDS = 2.0


def _resolve_bounded(host: str, port: int):
    """``socket.getaddrinfo`` borné par un timeout (HM-11).

    Retourne la liste d'infos getaddrinfo, ou lève ``TimeoutError`` /
    ``socket.gaierror``. Le thread de résolution n'est PAS attendu au shutdown
    (``wait=False``) pour que le timeout soit effectif même si getaddrinfo
    (appel C bloquant) est encore en cours.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(
            socket.getaddrinfo, host, port, 0, 0, socket.IPPROTO_TCP
        )
        return future.result(timeout=_DNS_RESOLVE_TIMEOUT_SECONDS)
    finally:
        executor.shutdown(wait=False)


def _blocked_ip_reason(ip: "ipaddress._BaseAddress", hostname: str) -> Optional[str]:
    """Retourne un message de refus si l'IP tombe dans une plage sensible,
    sinon None. Ordre du plus spécifique au plus général (message précis)."""
    if ip.is_loopback:
        return f"A loopback IP is not allowed for Graph Memory: {hostname}"
    if ip.is_link_local:
        return (
            f"A link-local IP is not allowed for Graph Memory: {hostname} "
            "(cloud metadata could be exposed)"
        )
    if ip.is_unspecified:
        return f"Unspecified IP address is forbidden for Graph Memory: {hostname}"
    if ip.is_multicast:
        return f"Multicast IP is not allowed for Graph Memory: {hostname}"
    if ip.is_reserved:
        return f"Reserved IP address is forbidden for Graph Memory: {hostname}"
    if ip.is_private:
        return f"Private IP address is forbidden for Graph Memory: {hostname}"
    return None


def validate_gm_url(url: str, *, allow_private_hosts: bool = False) -> Optional[str]:
    """
    Valide une URL de Graph Memory pour prévenir le SSRF.

    Retourne None si l'URL est sûre, sinon un message d'erreur explicite
    qui sera renvoyé tel quel à l'appelant (pas de fuite d'info sensible
    — on dit juste ce qui est invalide).

    ``allow_private_hosts=True`` : mode CONFIANCE (fix Codex #150, Finding 1).
    Réservé aux URLs de CONFIG OPÉRATEUR — au premier chef le runtime « long »
    embarqué interne (``LONG_EMBEDDED_URL``, défaut ``http://graph-memory:8002``),
    qui pointe LÉGITIMEMENT vers une IP privée du réseau Docker. Dans ce mode on
    saute le blocage d'IP privée ET la résolution DNS. À NE JAMAIS utiliser pour
    une URL fournie par un utilisateur (``graph_connect``) : ce serait rouvrir le
    SSRF. Même niveau de confiance que les URLs S3 / LLMaaS (non gardées).

    Politique :
    - scheme : http ou https uniquement (interdit file://, gopher://, ...)
    - hostname : présent et non vide
    - si hostname est une IP littérale :
      - les IPs privées (RFC 1918) sont bloquées
      - les IPs loopback (127.0.0.0/8) sont bloquées
      - les IPs link-local (169.254.0.0/16 → metadata cloud AWS/GCP/Azure)
        sont bloquées
      - les IPs unspecified (0.0.0.0) sont bloquées
      - les IPs multicast sont bloquées
    - si hostname est un DNS : accepté tel quel (la résolution est confiée
      au DNS du conteneur ; pour un anti-SSRF plus strict il faudrait
      résoudre le DNS et valider l'IP résolue, mais cela introduit une
      TOCTOU et n'est pas couvert par cette mitigation initiale).
    """
    if not url or not url.strip():
        return "Graph Memory URL is required"

    try:
        u = urlparse(url.strip())
    except Exception:
        return f"Invalid Graph Memory URL: '{url[:80]}'"

    if u.scheme not in _ALLOWED_GM_SCHEMES:
        return (
            f"URL scheme is not allowed for Graph Memory: '{u.scheme}'. "
            f"Expected: {', '.join(_ALLOWED_GM_SCHEMES)}."
        )

    if not u.hostname:
        return "A hostname is required in the Graph Memory URL"

    # Si c'est une IP littérale, on bloque directement les ranges sensibles.
    try:
        ip = ipaddress.ip_address(u.hostname)
    except ValueError:
        ip = None

    if ip is not None:
        # Finding 1 (Codex #150) : en mode CONFIANCE, une IP privée littérale est
        # légitime (URL interne opérateur).
        if allow_private_hosts:
            return None
        return _blocked_ip_reason(ip, u.hostname)

    # Finding 1 : mode CONFIANCE → l'URL interne opérateur (ex. graph-memory) est
    # un nom DNS qui résout vers une IP privée Docker BY DESIGN. On ne résout pas
    # et on ne bloque pas : sinon le runtime « long » embarqué P7 (ADR-0019) ne
    # démarre jamais. Le blocage SSRF ne concerne que les URLs NON fiables.
    if allow_private_hosts:
        return None

    # HM-11 fix : hostname DNS NON fiable (graph_connect) — l'ancienne version
    # l'acceptait TEL QUEL, ce qui laissait un contournement trivial (un nom qui
    # résout vers 169.254.169.254 ou une IP interne passait). On résout et on
    # valide TOUTES les IP résolues. Fail-closed si UNE des IP est sensible.
    try:
        infos = _resolve_bounded(
            u.hostname,
            u.port or (443 if u.scheme == "https" else 80),
        )
    except concurrent.futures.TimeoutError:
        # Finding 2 (Codex #150) : une résolution qui TIMEOUT est INDÉTERMINÉE et
        # suspecte (DNS lent potentiellement contrôlé par l'appelant). Pour une
        # URL non fiable, on échoue FERMÉ — on ne peut pas vérifier que l'IP
        # cible est sûre, donc on refuse plutôt que de laisser la connexion se
        # faire vers une IP non validée.
        _logger.warning(
            "Graph Memory URL: DNS resolution for %r timed out and was rejected "
            "(fail closed; target IP could not be verified).",
            u.hostname,
        )
        return (
            f"DNS resolution timed out for Graph Memory host {u.hostname!r}; "
            "rejected because the target IP could not be verified."
        )
    except socket.gaierror as e:
        # Finding 3 (Codex #150, round 2) : ne fail-OPEN QUE sur une non-résolution
        # DÉTERMINISTE (NXDOMAIN : EAI_NONAME / EAI_NODATA) — le nom n'a PAS d'IP,
        # ne peut donc atteindre aucune IP privée, et c'est le contrat « DNS =
        # blackbox » (test_accepts_public_dns_hostnames). Une erreur TRANSITOIRE /
        # indéterminée (EAI_AGAIN « temporary failure », EAI_FAIL, …) signifie
        # qu'on N'A PAS PU vérifier l'IP → fail-CLOSED pour une URL NON fiable
        # (sinon le client GM pourrait re-résoudre vers une IP privée ensuite).
        _deterministic = {
            getattr(socket, n)
            for n in ("EAI_NONAME", "EAI_NODATA")
            if hasattr(socket, n)
        }
        if e.errno in _deterministic:
            _logger.warning(
                "Graph Memory URL: host %r does not exist (NXDOMAIN); accepted "
                "under the DNS black-box contract. The operator egress allowlist remains the guard.",
                u.hostname,
            )
            return None
        _logger.warning(
            "Graph Memory URL: indeterminate DNS resolution for %r (errno=%s); "
            "rejected because the target IP could not be verified.",
            u.hostname,
            e.errno,
        )
        return (
            f"Indeterminate DNS resolution for Graph Memory host {u.hostname!r}; "
            "rejected because the target IP could not be verified."
        )
    except (socket.error, UnicodeError):
        # Erreur résolveur générique / indéterminée (pas un NXDOMAIN propre) →
        # fail-CLOSED pour une URL non fiable.
        _logger.warning(
            "Graph Memory URL: indeterminate resolution error for %r; "
            "rejected fail closed.",
            u.hostname,
        )
        return (
            f"DNS resolution failed for Graph Memory host {u.hostname!r}; "
            "rejected because the target IP could not be verified."
        )

    for info in infos:
        # Strip d'un éventuel zone-id IPv6 (« fe80::1%eth0 ») avant parsing —
        # sinon ipaddress lève ValueError et l'adresse link-local serait sautée.
        addr = info[4][0].split("%")[0]
        try:
            resolved = ipaddress.ip_address(addr)
        except ValueError:
            continue
        reason = _blocked_ip_reason(resolved, u.hostname)
        if reason:
            return f"{reason} (resolved from DNS host {u.hostname!r})"

    return None
