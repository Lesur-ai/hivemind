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
from typing import Optional, List, Tuple
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


@dataclass 
class SentenceGroup:
    """Groupe de phrases formant un chunk potentiel."""
    sentences: List[str] = field(default_factory=list)
    section_title: Optional[str] = None
    article_number: Optional[str] = None
    heading_hierarchy: List[str] = field(default_factory=list)
    
    @property
    def text(self) -> str:
        return " ".join(self.sentences)
    
    @property
    def token_estimate(self) -> int:
        """Estimation grossière : ~1 token = ~4 caractères en français."""
        return len(self.text) // 4


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
        
        # Normaliser les fins de ligne
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        
        # === PASSE 1 : Détecter la structure ===
        sections = self._detect_sections(text)
        
        total_chars = sum(len(s.content) for s in sections)
        print(f"📐 [Chunker] PASSE 1/3 — {len(sections)} sections détectées dans '{filename}' ({total_chars} chars)", file=sys.stderr)
        sys.stderr.flush()
        for i, s in enumerate(sections):
            art = f" (Art. {s.article_number})" if s.article_number else ""
            print(f"   📄 [{i+1}/{len(sections)}] {s.title[:70]}{art} — {len(s.content)} chars, level={s.level}", file=sys.stderr)
        sys.stderr.flush()
        
        # === PASSE 2 : Découper chaque section en phrases ===
        print(f"📐 [Chunker] PASSE 2/3 — Découpage en phrases...", file=sys.stderr)
        sys.stderr.flush()
        sentence_groups = self._sections_to_sentence_groups(sections)
        total_sentences = sum(len(g.sentences) for g in sentence_groups)
        print(f"📐 [Chunker] PASSE 2/3 — {total_sentences} phrases dans {len(sentence_groups)} groupes", file=sys.stderr)
        sys.stderr.flush()
        
        # === PASSE 3 : Regrouper les phrases en chunks avec overlap ===
        print(f"📐 [Chunker] PASSE 3/3 — Fusion en chunks (cible: {self._chunk_size} tokens, overlap: {self._chunk_overlap})...", file=sys.stderr)
        sys.stderr.flush()
        raw_chunks = self._merge_into_chunks(sentence_groups)
        print(f"📐 [Chunker] PASSE 3/3 — {len(raw_chunks)} chunks bruts générés", file=sys.stderr)
        sys.stderr.flush()
        
        # === Finaliser les Chunk avec métadonnées ===
        total = len(raw_chunks)
        chunks = []
        for i, (group, chunk_text) in enumerate(raw_chunks):
            chunk = Chunk(
                text=chunk_text.strip(),
                index=i,
                total_chunks=total,
                filename=filename,
                section_title=group.section_title,
                article_number=group.article_number,
                heading_hierarchy=group.heading_hierarchy,
                char_count=len(chunk_text.strip()),
                token_estimate=len(chunk_text.strip()) // 4
            )
            chunks.append(chunk)
        
        print(f"✅ [Chunker] '{filename}' → {len(chunks)} chunks "
              f"(cible: {self._chunk_size} tokens, overlap: {self._chunk_overlap})", file=sys.stderr)
        
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
            preamble = text[:matches[0].start()].strip()
            if preamble:
                sections.append(TextSection(
                    title="Préambule",
                    content=preamble,
                    level=0,
                    start_pos=0
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
            
            content = text[start:end].strip()
            
            sections.append(TextSection(
                title=title,
                content=content,
                level=0,
                article_number=article_num,
                start_pos=start
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
            preamble = text[:matches[0].start()].strip()
            if preamble:
                sections.append(TextSection(
                    title="Introduction",
                    content=preamble,
                    level=0,
                    start_pos=0
                ))
        
        for i, match in enumerate(matches):
            level = len(match.group(1))  # Nombre de #
            title = match.group(2).strip()
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            content = text[start:end].strip()
            
            sections.append(TextSection(
                title=title,
                content=content,
                level=level - 1,  # ## = level 1, ### = level 2
                start_pos=start
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
            preamble = text[:matches[0].start()].strip()
            if preamble:
                sections.append(TextSection(
                    title="Introduction",
                    content=preamble,
                    level=0,
                    start_pos=0
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
            
            content = text[start:end].strip()
            
            sections.append(TextSection(
                title=title,
                content=content,
                level=level,
                article_number=num,
                start_pos=start
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
            preamble = text[:matches[0].start()].strip()
            if preamble:
                sections.append(TextSection(
                    title="Introduction",
                    content=preamble,
                    level=0,
                    start_pos=0
                ))
        
        for i, match in enumerate(matches):
            title = match.group(1).strip()
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            content = text[start:end].strip()
            
            sections.append(TextSection(
                title=title,
                content=content,
                level=0,
                start_pos=start
            ))
        
        return sections
    
    def _detect_paragraphs(self, text: str) -> List[TextSection]:
        """Fallback : découpe par double saut de ligne."""
        paragraphs = re.split(r'\n\s*\n', text)
        sections = []
        
        for i, para in enumerate(paragraphs):
            para = para.strip()
            if not para:
                continue
            
            # Utiliser la première ligne comme titre (tronquée)
            first_line = para.split('\n')[0][:80]
            
            sections.append(TextSection(
                title=first_line,
                content=para,
                level=0,
                start_pos=0  # Approximatif
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
            sentences = self._split_into_sentences(section.content)
            
            if sentences:
                groups.append(SentenceGroup(
                    sentences=sentences,
                    section_title=section.title,
                    article_number=section.article_number,
                    heading_hierarchy=list(heading_stack)
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
        if not text.strip():
            return []
        
        # D'abord, séparer les éléments de liste (tirets, puces, numérotation)
        lines = text.split('\n')
        sentences = []
        current_sentence = []
        
        for line in lines:
            line = line.strip()
            if not line:
                # Ligne vide = fin de phrase en cours
                if current_sentence:
                    sentences.append(' '.join(current_sentence))
                    current_sentence = []
                continue
            
            # Détecter les éléments de liste (tiret, puce, numéro suivi de point/parenthèse)
            is_list_item = bool(re.match(r'^[-•●▪]\s+', line)) or \
                           bool(re.match(r'^\d+[.)]\s+', line)) or \
                           bool(re.match(r'^[a-z][.)]\s+', line))
            
            if is_list_item:
                # Sauver la phrase en cours
                if current_sentence:
                    sentences.append(' '.join(current_sentence))
                    current_sentence = []
                # L'élément de liste est une phrase à part entière
                sentences.append(line)
            else:
                # Ajouter à la phrase en cours
                current_sentence.append(line)
                
                # Si la ligne se termine par un point/!/?
                if re.search(r'[.!?]\s*$', line):
                    sentences.append(' '.join(current_sentence))
                    current_sentence = []
        
        # Dernière phrase en cours
        if current_sentence:
            sentences.append(' '.join(current_sentence))
        
        # Deuxième passe : re-découper les phrases trop longues
        final_sentences = []
        for sent in sentences:
            if len(sent) > 1500:  # ~375 tokens, trop long pour une phrase
                # Découper sur les points internes
                sub_sentences = SENTENCE_ENDINGS.split(sent)
                final_sentences.extend(sub_sentences)
            else:
                final_sentences.append(sent)
        
        # Filtrer les phrases vides
        return [s.strip() for s in final_sentences if s.strip()]
    
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
        
        if not sentences:
            return []
        
        current_sentences: List[str] = []
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
                    heading_hierarchy=group.heading_hierarchy
                )
                chunk_text = context_prefix + sent
                chunks.append((sub_group, chunk_text))
                i += 1
                continue
            
            # Ajouter la phrase si elle tient
            if current_tokens + sent_tokens + prefix_tokens <= self._chunk_size:
                current_sentences.append(sent)
                current_tokens += sent_tokens
                i += 1
            else:
                # Finaliser le chunk courant
                if current_sentences:
                    sub_group = SentenceGroup(
                        sentences=list(current_sentences),
                        section_title=group.section_title,
                        article_number=group.article_number,
                        heading_hierarchy=group.heading_hierarchy
                    )
                    chunk_text = context_prefix + " ".join(current_sentences)
                    chunks.append((sub_group, chunk_text))
                    
                    # Overlap : reprendre les dernières phrases
                    overlap_sentences = self._compute_overlap(current_sentences)
                    overlap_tokens = sum(len(s) // 4 for s in overlap_sentences)
                    
                    # PROTECTION BOUCLE INFINIE : si l'overlap + prochaine phrase
                    # dépasse la taille cible, on FORCE l'avancement en vidant l'overlap
                    if overlap_tokens + sent_tokens + prefix_tokens > self._chunk_size:
                        # La phrase est trop grosse même avec juste l'overlap → on prend
                        # la phrase seule dans le prochain chunk (sans overlap)
                        current_sentences = []
                        current_tokens = 0
                    else:
                        current_sentences = overlap_sentences
                        current_tokens = overlap_tokens
                else:
                    i += 1  # Éviter boucle infinie
        
        # Dernier chunk
        if current_sentences:
            sub_group = SentenceGroup(
                sentences=list(current_sentences),
                section_title=group.section_title,
                article_number=group.article_number,
                heading_hierarchy=group.heading_hierarchy
            )
            chunk_text = context_prefix + " ".join(current_sentences)
            chunks.append((sub_group, chunk_text))
        
        return chunks
    
    def _compute_overlap(self, sentences: List[str]) -> List[str]:
        """
        Calcule les phrases d'overlap (dernières phrases du chunk précédent).
        
        Prend les dernières phrases jusqu'à atteindre chunk_overlap tokens.
        Ne coupe jamais une phrase.
        """
        if not sentences or self._chunk_overlap <= 0:
            return []
        
        overlap = []
        overlap_tokens = 0
        
        for sent in reversed(sentences):
            sent_tokens = len(sent) // 4
            if overlap_tokens + sent_tokens > self._chunk_overlap:
                break
            overlap.insert(0, sent)
            overlap_tokens += sent_tokens
        
        return overlap
    
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
