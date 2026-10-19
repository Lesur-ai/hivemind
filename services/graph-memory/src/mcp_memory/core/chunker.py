# -*- coding: utf-8 -*-
"""
SemanticChunker - Découpage sémantique des documents.

Stratégie de chunking respectant les frontières naturelles du texte :
1. Détecte la structure : articles numérotés, headers Markdown, titres en majuscules
2. Découpe par sections sémantiques (articles, paragraphes)
3. Ne coupe JAMAIS au milieu d'une phrase
4. Regroupe les petits blocs, sous-découpe les gros
5. Overlap intelligent entre chunks adjacents (contexte aux frontières)
6. Chaque chunk porte ses métadonnées (section, article, hiérarchie)

Best practices implémentées :
- Recursive splitting : séparateurs du plus structurel au plus fin
- Sentence-level granularity : la phrase est l'unité atomique
- Context preservation : titre de section/article en préfixe
- Overlap at sentence boundaries : l'overlap ne coupe pas les phrases
"""

import re
import sys
from bisect import bisect_right
from typing import Callable, Optional, List, Tuple
from dataclasses import dataclass, field

from ..config import get_settings
from .models import Chunk


# =============================================================================
# Patterns de détection de structure
# =============================================================================

# Articles numérotés (juridique) : "Article 1", "Article 23.2", "ARTICLE 1er"
ARTICLE_PATTERN = re.compile(
    r'^(?:ARTICLE|Article|article)\s+(\d+(?:\.\d+)*(?:\s*(?:er|ème|eme))?)\s*[:\.\s–—-]',
    re.MULTILINE
)

# Numérotation hiérarchique : "1.", "1.1", "1.1.1", "23.2 –"
NUMBERED_SECTION_PATTERN = re.compile(
    r'^(\d+(?:\.\d+)+)\s*[:\.\s–—-]',
    re.MULTILINE
)

# Headers Markdown : "## Titre", "### Sous-titre"
MARKDOWN_HEADER_PATTERN = re.compile(
    r'^(#{1,6})\s+(.+)$',
    re.MULTILINE
)

# Titres en majuscules (minimum 4 mots, pas juste un acronyme)
UPPERCASE_TITLE_PATTERN = re.compile(
    r'^([A-ZÀÂÄÉÈÊËÏÎÔÙÛÜŸÇ][A-ZÀÂÄÉÈÊËÏÎÔÙÛÜŸÇ\s,\'-]{15,})$',
    re.MULTILINE
)

# Séparateurs de phrases (pour le split au niveau phrase)
SENTENCE_ENDINGS = re.compile(
    r'(?<=[.!?])\s+(?=[A-ZÀÂÄÉÈÊËÏÎÔÙÛÜŸ])'
)


@dataclass
class TextSection:
    """Section détectée dans le document."""
    title: str
    content: str
    level: int = 0  # 0 = article/section principale, 1 = sous-section, etc.
    article_number: Optional[str] = None
    start_pos: int = 0
    # Offset of ``content`` in the line-ending-normalized text.
    content_start: int = 0


@dataclass
class SentenceGroup:
    """Groupe de phrases formant un chunk potentiel."""
    sentences: List[str] = field(default_factory=list)
    section_title: Optional[str] = None
    article_number: Optional[str] = None
    heading_hierarchy: List[str] = field(default_factory=list)
    # (start, end) of each sentence in the normalized text, parallel to sentences.
    spans: List[Tuple[int, int]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(self.sentences)
    
    @property
    def token_estimate(self) -> int:
        """Estimation grossière : ~1 token = ~4 caractères en français."""
        return len(self.text) // 4


def _stripped_range(text: str, start: int, end: int) -> Tuple[int, int]:
    """Range of ``text[start:end].strip()`` inside ``text`` (same characters)."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _source_mapper(original: str) -> Callable[[int], int]:
    """Map offsets of the line-ending-normalized text to ``original``.

    Only a CR directly followed by LF is removed by the normalization; a lone
    CR becomes LF at the same offset.
    """
    if "\r" not in original:
        return lambda offset: offset
    deleted = [i for i in range(len(original) - 1) if original[i] == "\r" and original[i + 1] == "\n"]
    slots = [index - rank for rank, index in enumerate(deleted)]
    return lambda offset: offset + bisect_right(slots, offset)


class SemanticChunker:
    """
    Chunker sémantique respectant les frontières naturelles du texte.
    
    Stratégie en 3 passes :
    1. DETECT : Identifier les sections structurelles (articles, titres, headers)
    2. SPLIT  : Découper chaque section en phrases
    3. MERGE  : Regrouper les phrases en chunks de taille cible avec overlap
    """
    
    def __init__(self):
        """Initialise le chunker avec les paramètres de configuration."""
        settings = get_settings()
        self._chunk_size = settings.chunk_size  # tokens
        self._chunk_overlap = settings.chunk_overlap  # tokens

    def configuration_signature(self) -> tuple[int, int]:
        """Return the immutable values that determine chunk accounting."""
        return (self._chunk_size, self._chunk_overlap)
    
    def chunk_document(self, text: str, filename: str) -> List[Chunk]:
        """
        Découpe un document en chunks sémantiques.
        
        Args:
            text: Contenu textuel complet du document
            filename: Nom du fichier (pour les métadonnées)
            
        Returns:
            Liste de Chunk avec métadonnées sémantiques
        """
        if not text or not text.strip():
            return []

        # Normaliser les fins de ligne (offsets d'origine conservés pour les plages source)
        to_source = _source_mapper(text)
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        
        # === PASSE 1 : Détecter la structure ===
        sections = self._detect_sections(text)
        
        total_chars = sum(len(s.content) for s in sections)
        print(f"📐 [Chunker] PASS 1/3 — {len(sections)} sections detected in '{filename}' ({total_chars} chars)", file=sys.stderr)
        sys.stderr.flush()
        for i, s in enumerate(sections):
            art = f" (Art. {s.article_number})" if s.article_number else ""
            print(f"   📄 [{i+1}/{len(sections)}] {s.title[:70]}{art} — {len(s.content)} chars, level={s.level}", file=sys.stderr)
        sys.stderr.flush()
        
        # === PASSE 2 : Découper chaque section en phrases ===
        print("📐 [Chunker] PASS 2/3 — Splitting into sentences...", file=sys.stderr)
        sys.stderr.flush()
        sentence_groups = self._sections_to_sentence_groups(sections)
        total_sentences = sum(len(g.sentences) for g in sentence_groups)
        print(f"📐 [Chunker] PASS 2/3 — {total_sentences} sentences in {len(sentence_groups)} groups", file=sys.stderr)
        sys.stderr.flush()
        
        # === PASSE 3 : Regrouper les phrases en chunks avec overlap ===
        print(f"📐 [Chunker] PASS 3/3 — Merging into chunks (target: {self._chunk_size} tokens, overlap: {self._chunk_overlap})...", file=sys.stderr)
        sys.stderr.flush()
        raw_chunks = self._merge_into_chunks(sentence_groups)
        print(f"📐 [Chunker] PASS 3/3 — {len(raw_chunks)} raw chunks generated", file=sys.stderr)
        sys.stderr.flush()
        
        # === Finaliser les Chunk avec métadonnées ===
        total = len(raw_chunks)
        chunks = []
        for i, (group, chunk_text) in enumerate(raw_chunks):
            # One contiguous source range per passage: first sentence start to
            # last sentence end, mapped to the exact extracted text offsets.
            source_spans = []
            if group.spans:
                first = min(start for start, _ in group.spans)
                last = max(end for _, end in group.spans)
                source_spans = [{"start": to_source(first), "end": to_source(last - 1) + 1}]
            chunk = Chunk(
                text=chunk_text.strip(),
                index=i,
                total_chunks=total,
                filename=filename,
                section_title=group.section_title,
                article_number=group.article_number,
                heading_hierarchy=group.heading_hierarchy,
                char_count=len(chunk_text.strip()),
                token_estimate=len(chunk_text.strip()) // 4,
                source_spans=source_spans,
            )
            chunks.append(chunk)
        
        print(f"✅ [Chunker] '{filename}' → {len(chunks)} chunks "
              f"(target: {self._chunk_size} tokens, overlap: {self._chunk_overlap})", file=sys.stderr)
        
        return chunks
    
    # =========================================================================
    # PASSE 1 : Détection de la structure
    # =========================================================================
    
    def _detect_sections(self, text: str) -> List[TextSection]:
        """
        Détecte les sections structurelles du document.
        
        Ordre de priorité :
        1. Articles numérotés (juridique)
        2. Headers Markdown
        3. Numérotation hiérarchique (1.1, 1.1.1)
        4. Titres en majuscules
        5. Double saut de ligne (paragraphes)
        
        Si aucune structure détectée, retourne le texte entier comme section unique.
        """
        # Essayer de détecter des articles numérotés (documents juridiques)
        sections = self._detect_articles(text)
        if sections and len(sections) > 1:
            return sections
        
        # Essayer les headers Markdown
        sections = self._detect_markdown_headers(text)
        if sections and len(sections) > 1:
            return sections
        
        # Essayer la numérotation hiérarchique
        sections = self._detect_numbered_sections(text)
        if sections and len(sections) > 1:
            return sections
        
        # Essayer les titres en majuscules
        sections = self._detect_uppercase_titles(text)
        if sections and len(sections) > 1:
            return sections
        
        # Fallback : découper par double saut de ligne (paragraphes)
        sections = self._detect_paragraphs(text)
        return sections
    
    def _detect_articles(self, text: str) -> List[TextSection]:
        """Détecte les articles numérotés (documents juridiques)."""
        matches = list(ARTICLE_PATTERN.finditer(text))
        if not matches:
            return []
        
        sections = []
        
        # Texte avant le premier article (préambule)
        if matches[0].start() > 0:
            c_start, c_end = _stripped_range(text, 0, matches[0].start())
            if c_end > c_start:
                sections.append(TextSection(
                    title="Preamble",
                    content=text[c_start:c_end],
                    level=0,
                    start_pos=0,
                    content_start=c_start
                ))

        # Chaque article
        for i, match in enumerate(matches):
            article_num = match.group(1).strip()
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)

            # Le titre de l'article = la première ligne
            first_line_end = text.find('\n', start)
            if first_line_end == -1:
                first_line_end = end
            title = text[start:first_line_end].strip()

            c_start, c_end = _stripped_range(text, start, end)

            sections.append(TextSection(
                title=title,
                content=text[c_start:c_end],
                level=0,
                article_number=article_num,
                start_pos=start,
                content_start=c_start
            ))

        return sections
    
    def _detect_markdown_headers(self, text: str) -> List[TextSection]:
        """Détecte les headers Markdown (## Titre)."""
        matches = list(MARKDOWN_HEADER_PATTERN.finditer(text))
        if not matches:
            return []
        
        sections = []
        
        # Texte avant le premier header
        if matches[0].start() > 0:
            c_start, c_end = _stripped_range(text, 0, matches[0].start())
            if c_end > c_start:
                sections.append(TextSection(
                    title="Introduction",
                    content=text[c_start:c_end],
                    level=0,
                    start_pos=0,
                    content_start=c_start
                ))

        for i, match in enumerate(matches):
            level = len(match.group(1))  # Nombre de #
            title = match.group(2).strip()
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            c_start, c_end = _stripped_range(text, start, end)

            sections.append(TextSection(
                title=title,
                content=text[c_start:c_end],
                level=level - 1,  # ## = level 1, ### = level 2
                start_pos=start,
                content_start=c_start
            ))

        return sections
    
    def _detect_numbered_sections(self, text: str) -> List[TextSection]:
        """Détecte les sections numérotées (1.1, 1.1.1, etc.)."""
        matches = list(NUMBERED_SECTION_PATTERN.finditer(text))
        if not matches:
            return []
        
        sections = []
        
        # Texte avant la première section
        if matches[0].start() > 0:
            c_start, c_end = _stripped_range(text, 0, matches[0].start())
            if c_end > c_start:
                sections.append(TextSection(
                    title="Introduction",
                    content=text[c_start:c_end],
                    level=0,
                    start_pos=0,
                    content_start=c_start
                ))

        for i, match in enumerate(matches):
            num = match.group(1)
            level = num.count('.')  # 1.1 = level 1, 1.1.1 = level 2
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)

            # Titre = première ligne
            first_line_end = text.find('\n', start)
            if first_line_end == -1 or first_line_end > end:
                first_line_end = end
            title = text[start:first_line_end].strip()

            c_start, c_end = _stripped_range(text, start, end)

            sections.append(TextSection(
                title=title,
                content=text[c_start:c_end],
                level=level,
                article_number=num,
                start_pos=start,
                content_start=c_start
            ))

        return sections
    
    def _detect_uppercase_titles(self, text: str) -> List[TextSection]:
        """Détecte les titres en majuscules."""
        matches = list(UPPERCASE_TITLE_PATTERN.finditer(text))
        if not matches:
            return []
        
        sections = []
        
        # Texte avant le premier titre
        if matches[0].start() > 0:
            c_start, c_end = _stripped_range(text, 0, matches[0].start())
            if c_end > c_start:
                sections.append(TextSection(
                    title="Introduction",
                    content=text[c_start:c_end],
                    level=0,
                    start_pos=0,
                    content_start=c_start
                ))

        for i, match in enumerate(matches):
            title = match.group(1).strip()
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            c_start, c_end = _stripped_range(text, start, end)

            sections.append(TextSection(
                title=title,
                content=text[c_start:c_end],
                level=0,
                start_pos=start,
                content_start=c_start
            ))

        return sections

    def _detect_paragraphs(self, text: str) -> List[TextSection]:
        """Fallback : découpe par double saut de ligne."""
        # Same segmentation as re.split(r'\n\s*\n', text), with offsets.
        bounds = []
        cursor = 0
        for separator in re.finditer(r'\n\s*\n', text):
            bounds.append((cursor, separator.start()))
            cursor = separator.end()
        bounds.append((cursor, len(text)))
        sections = []

        for seg_start, seg_end in bounds:
            c_start, c_end = _stripped_range(text, seg_start, seg_end)
            if c_end <= c_start:
                continue
            para = text[c_start:c_end]

            # Utiliser la première ligne comme titre (tronquée)
            first_line = para.split('\n')[0][:80]

            sections.append(TextSection(
                title=first_line,
                content=para,
                level=0,
                start_pos=0,  # Approximatif
                content_start=c_start
            ))

        return sections
    
    # =========================================================================
    # PASSE 2 : Sections → Groupes de phrases
    # =========================================================================
    
    def _sections_to_sentence_groups(self, sections: List[TextSection]) -> List[SentenceGroup]:
        """
        Convertit les sections en groupes de phrases avec métadonnées.
        
        Chaque section est découpée en phrases individuelles.
        Les phrases sont regroupées avec les métadonnées de leur section.
        """
        groups = []
        heading_stack: List[str] = []  # Pile de titres pour la hiérarchie
        
        for section in sections:
            # Maintenir la hiérarchie des titres
            # On enlève les titres de même niveau ou supérieur
            while len(heading_stack) > section.level:
                heading_stack.pop()
            heading_stack.append(section.title)
            
            # Découper le contenu en phrases
            located = self._split_into_sentence_spans(section.content, section.content_start)

            if located:
                groups.append(SentenceGroup(
                    sentences=[sentence for sentence, _, _ in located],
                    section_title=section.title,
                    article_number=section.article_number,
                    heading_hierarchy=list(heading_stack),
                    spans=[(start, end) for _, start, end in located]
                ))

        return groups

    def _split_into_sentences(self, text: str) -> List[str]:
        """
        Découpe un texte en phrases.

        Respecte les frontières naturelles :
        - Points, points d'exclamation, points d'interrogation
        - Ne coupe pas sur les abréviations courantes (M., etc.)
        - Ne coupe pas sur les numéros (art. 23.2)
        - Garde les listes à puces comme phrases individuelles
        """
        return [sentence for sentence, _, _ in self._split_into_sentence_spans(text, 0)]

    def _split_into_sentence_spans(self, text: str, base: int) -> List[Tuple[str, int, int]]:
        """Sentences of ``text`` with their (start, end) offsets, shifted by ``base``.

        The sentence strings are exactly those of the historical splitter; the
        ranges point at the source characters each sentence was built from,
        before lines are stripped and joined.
        """
        if not text.strip():
            return []

        # D'abord, séparer les éléments de liste (tirets, puces, numérotation)
        lines = text.split('\n')
        # Each pending sentence is a list of (stripped line, absolute start).
        sentences: List[List[Tuple[str, int]]] = []
        current_sentence: List[Tuple[str, int]] = []
        line_offset = base

        for raw_line in lines:
            line = raw_line.strip()
            line_start = line_offset + (len(raw_line) - len(raw_line.lstrip()))
            line_offset += len(raw_line) + 1
            if not line:
                # Ligne vide = fin de phrase en cours
                if current_sentence:
                    sentences.append(current_sentence)
                    current_sentence = []
                continue

            # Détecter les éléments de liste (tiret, puce, numéro suivi de point/parenthèse)
            is_list_item = bool(re.match(r'^[-•●▪]\s+', line)) or \
                           bool(re.match(r'^\d+[.)]\s+', line)) or \
                           bool(re.match(r'^[a-z][.)]\s+', line))

            if is_list_item:
                # Sauver la phrase en cours
                if current_sentence:
                    sentences.append(current_sentence)
                    current_sentence = []
                # L'élément de liste est une phrase à part entière
                sentences.append([(line, line_start)])
            else:
                # Ajouter à la phrase en cours
                current_sentence.append((line, line_start))

                # Si la ligne se termine par un point/!/?
                if re.search(r'[.!?]\s*$', line):
                    sentences.append(current_sentence)
                    current_sentence = []

        # Dernière phrase en cours
        if current_sentence:
            sentences.append(current_sentence)

        # Deuxième passe : re-découper les phrases trop longues
        located: List[Tuple[str, int, int]] = []
        for parts in sentences:
            sent = ' '.join(line for line, _ in parts)
            if len(sent) > 1500:  # ~375 tokens, trop long pour une phrase
                # Découper sur les points internes
                pieces = []
                cursor = 0
                for separator in SENTENCE_ENDINGS.finditer(sent):
                    pieces.append((cursor, separator.start()))
                    cursor = separator.end()
                pieces.append((cursor, len(sent)))
            else:
                pieces = [(0, len(sent))]
            for piece_start, piece_end in pieces:
                piece_start, piece_end = _stripped_range(sent, piece_start, piece_end)
                if piece_end <= piece_start:
                    continue
                # Map joined-string offsets back to source characters.
                first = self._joined_to_source(parts, piece_start)
                last = self._joined_to_source(parts, piece_end - 1)
                located.append((sent[piece_start:piece_end], first, last + 1))

        # Filtrer les phrases vides
        return located

    @staticmethod
    def _joined_to_source(parts: List[Tuple[str, int]], position: int) -> int:
        """Absolute source offset of character ``position`` of ``' '.join(lines)``."""
        offset = 0
        for line, start in parts:
            if position < offset + len(line):
                return start + (position - offset)
            offset += len(line) + 1
        raise ValueError("position outside the joined sentence")
    
    # =========================================================================
    # PASSE 3 : Groupes de phrases → Chunks avec overlap
    # =========================================================================
    
    def _merge_into_chunks(
        self, 
        groups: List[SentenceGroup]
    ) -> List[Tuple[SentenceGroup, str]]:
        """
        Regroupe les phrases en chunks de taille cible avec overlap.
        
        Algorithme :
        1. Pour chaque groupe de phrases (section), on accumule les phrases
        2. Quand on atteint chunk_size tokens, on finalise le chunk
        3. On garde les dernières phrases (overlap) comme début du chunk suivant
        4. Si une section entière tient dans un chunk, on la garde intacte
        
        Returns:
            Liste de (SentenceGroup metadata, texte du chunk)
        """
        if not groups:
            return []
        
        chunks: List[Tuple[SentenceGroup, str]] = []
        
        for group in groups:
            # Si le groupe entier tient dans un chunk, on le garde tel quel
            group_tokens = group.token_estimate
            
            if group_tokens <= self._chunk_size:
                # Section entière = un chunk (préserve l'unité sémantique)
                # Ajouter le titre comme contexte
                chunk_text = self._format_chunk_with_context(group)
                chunks.append((group, chunk_text))
            else:
                # Section trop longue → sous-découper avec overlap
                sub_chunks = self._split_group_with_overlap(group)
                chunks.extend(sub_chunks)
        
        return chunks
    
    def _split_group_with_overlap(
        self, 
        group: SentenceGroup
    ) -> List[Tuple[SentenceGroup, str]]:
        """
        Sous-découpe un groupe de phrases trop long en chunks avec overlap.
        
        L'overlap se fait au niveau des phrases : on reprend les dernières
        phrases du chunk précédent comme début du chunk suivant.
        """
        chunks = []
        sentences = group.sentences
        spans = group.spans

        if not sentences:
            return []

        current_sentences: List[str] = []
        current_spans: List[Tuple[int, int]] = []
        current_tokens = 0
        
        # Préfixe contextuel (titre de section/article)
        context_prefix = ""
        if group.article_number:
            context_prefix = f"[Article {group.article_number}] "
        elif group.section_title:
            context_prefix = f"[{group.section_title[:60]}] "
        prefix_tokens = len(context_prefix) // 4
        
        i = 0
        while i < len(sentences):
            sent = sentences[i]
            sent_tokens = len(sent) // 4
            
            # Si une phrase unique dépasse chunk_size, on la prend quand même
            if not current_sentences and sent_tokens > self._chunk_size - prefix_tokens:
                sub_group = SentenceGroup(
                    sentences=[sent],
                    section_title=group.section_title,
                    article_number=group.article_number,
                    heading_hierarchy=group.heading_hierarchy,
                    spans=spans[i:i + 1]
                )
                chunk_text = context_prefix + sent
                chunks.append((sub_group, chunk_text))
                i += 1
                continue

            # Ajouter la phrase si elle tient
            if current_tokens + sent_tokens + prefix_tokens <= self._chunk_size:
                current_sentences.append(sent)
                current_spans.extend(spans[i:i + 1])
                current_tokens += sent_tokens
                i += 1
            else:
                # Finaliser le chunk courant
                if current_sentences:
                    sub_group = SentenceGroup(
                        sentences=list(current_sentences),
                        section_title=group.section_title,
                        article_number=group.article_number,
                        heading_hierarchy=group.heading_hierarchy,
                        spans=list(current_spans)
                    )
                    chunk_text = context_prefix + " ".join(current_sentences)
                    chunks.append((sub_group, chunk_text))

                    # Overlap : reprendre les dernières phrases
                    overlap_count = self._overlap_count(current_sentences)
                    overlap_sentences = current_sentences[len(current_sentences) - overlap_count:]
                    # current_spans is parallel to current_sentences (empty only for
                    # groups built without ranges).
                    overlap_spans = current_spans[len(current_spans) - overlap_count:] if overlap_count else []
                    overlap_tokens = sum(len(s) // 4 for s in overlap_sentences)

                    # PROTECTION BOUCLE INFINIE : si l'overlap + prochaine phrase
                    # dépasse la taille cible, on FORCE l'avancement en vidant l'overlap
                    if overlap_tokens + sent_tokens + prefix_tokens > self._chunk_size:
                        # La phrase est trop grosse même avec juste l'overlap → on prend
                        # la phrase seule dans le prochain chunk (sans overlap)
                        current_sentences = []
                        current_spans = []
                        current_tokens = 0
                    else:
                        current_sentences = overlap_sentences
                        current_spans = overlap_spans
                        current_tokens = overlap_tokens
                else:
                    i += 1  # Éviter boucle infinie

        # Dernier chunk
        if current_sentences:
            sub_group = SentenceGroup(
                sentences=list(current_sentences),
                section_title=group.section_title,
                article_number=group.article_number,
                heading_hierarchy=group.heading_hierarchy,
                spans=list(current_spans)
            )
            chunk_text = context_prefix + " ".join(current_sentences)
            chunks.append((sub_group, chunk_text))

        return chunks

    def _overlap_count(self, sentences: List[str]) -> int:
        """Number of trailing sentences the next chunk repeats as overlap."""
        if not sentences or self._chunk_overlap <= 0:
            return 0

        count = 0
        overlap_tokens = 0

        for sent in reversed(sentences):
            sent_tokens = len(sent) // 4
            if overlap_tokens + sent_tokens > self._chunk_overlap:
                break
            count += 1
            overlap_tokens += sent_tokens

        return count

    def _compute_overlap(self, sentences: List[str]) -> List[str]:
        """
        Calcule les phrases d'overlap (dernières phrases du chunk précédent).

        Prend les dernières phrases jusqu'à atteindre chunk_overlap tokens.
        Ne coupe jamais une phrase.
        """
        count = self._overlap_count(sentences)
        return sentences[len(sentences) - count:] if count else []
    
    def _format_chunk_with_context(self, group: SentenceGroup) -> str:
        """
        Formate un chunk avec son contexte hiérarchique en préfixe.
        
        Ex: "[Article 23.2 - Réversibilité] Le prestataire s'engage..."
        """
        prefix_parts = []
        
        if group.article_number:
            prefix_parts.append(f"Article {group.article_number}")
        
        if group.section_title and group.section_title != f"Article {group.article_number}":
            # Éviter la redondance si le titre est juste "Article X"
            clean_title = group.section_title
            # Tronquer les titres trop longs
            if len(clean_title) > 80:
                clean_title = clean_title[:77] + "..."
            prefix_parts.append(clean_title)
        
        prefix = ""
        if prefix_parts:
            prefix = "[" + " - ".join(prefix_parts) + "] "
        
        return prefix + " ".join(group.sentences)


# Singleton pour usage global
_chunker: Optional[SemanticChunker] = None


def get_chunker() -> SemanticChunker:
    """Retourne l'instance singleton du SemanticChunker."""
    global _chunker
    if _chunker is None:
        _chunker = SemanticChunker()
    return _chunker
