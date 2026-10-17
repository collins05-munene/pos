import re

from django.db.models import Q

from .models import FeatureRequest, HelpArticle

STOPWORDS = {
    "the", "and", "for", "not", "was", "are", "but", "with", "when", "this", "that",
    "have", "has", "can", "cant", "how", "why", "what", "from", "into", "after",
    "its", "all", "any", "get", "got", "does", "doesnt", "dont", "work", "working",
}


def _terms(q):
    seen = []
    for w in re.findall(r"[a-z0-9]+", (q or "").lower()):
        if len(w) > 2 and w not in STOPWORDS and w not in seen:
            seen.append(w)
    return seen[:8]


def search_articles(q, limit=8, kind=None, min_score=0.5):
    """
    Simple ranked search that works on any database: title hits count most,
    then keywords, summary and body. On PostgreSQL you can swap this for
    SearchVector/SearchRank later without touching the views.
    """
    terms = _terms(q)
    if not terms:
        return []
    cond = Q()
    for t in terms:
        cond |= (
            Q(title__icontains=t) | Q(keywords__icontains=t)
            | Q(summary__icontains=t) | Q(body__icontains=t)
        )
    qs = HelpArticle.objects.filter(is_published=True).filter(cond)
    if kind:
        qs = qs.filter(kind=kind)

    scored = []
    for a in qs[:80]:
        title, kw, summ, body = a.title.lower(), a.keywords.lower(), a.summary.lower(), a.body.lower()
        score = sum(
            3 * (t in title) + 2 * (t in kw) + 1 * (t in summ) + 0.5 * (t in body)
            for t in terms
        )
        if score >= min_score:
            scored.append((score, a))
    scored.sort(key=lambda s: (-s[0], s[1].title))
    return [a for _, a in scored[:limit]]


def search_features(q, limit=5):
    terms = _terms(q)
    if not terms:
        return []
    cond = Q()
    for t in terms:
        cond |= Q(title__icontains=t) | Q(description__icontains=t)
    qs = (
        FeatureRequest.objects.filter(is_visible=True)
        .exclude(status=FeatureRequest.Status.DECLINED)
        .filter(cond)
    )
    scored = []
    for f in qs[:80]:
        title = f.title.lower()
        score = sum(3 * (t in title) + 1 * (t in f.description.lower()) for t in terms)
        scored.append((score, f))
    scored.sort(key=lambda s: (-s[0], -s[1].vote_count))
    return [f for _, f in scored[:limit]]
