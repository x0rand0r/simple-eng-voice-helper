"""Prompts for speech recognition, translation and the speaking coach."""
from __future__ import annotations

import re

from .config import lang_name

NOTE_SEPARATOR = "###"
NO_TRANSLATION = "="

STYLES = {
    "minimal": "Fix only what is actually wrong (grammar, word choice, word order) and keep the learner's own "
               "words and sentence structure wherever possible.",
    "natural": "Make it sound natural and fluent, the way a native speaker would really say it in this "
               "conversation: idiomatic word choice, common collocations and phrasal verbs, contractions where "
               "natural. Keep roughly the same length and register.",
    "advanced": "Make it rich and idiomatic, the way an articulate native speaker would phrase it: vivid "
                "vocabulary, idioms, collocations and phrasal verbs. It may be longer than the original.",
}


def stt_prompt(kind: str, a: str, b: str) -> str:
    if kind == "me":
        return (f"The speaker is practising {lang_name(b)}; their native language is {lang_name(a)}. "
                "Transcribe verbatim, exactly as spoken, keeping grammar mistakes, repetitions and hesitations.")
    return f"A conversation in {lang_name(b)} or {lang_name(a)}."


def coach_system(a: str, b: str, style: str, explain: bool) -> str:
    la, lb = lang_name(a), lang_name(b)
    text = (
        f"You are a speaking coach. The learner's native language is {la}; they are practising {lb} in a live "
        "conversation. You get the recent conversation for context and the learner's latest utterance, "
        "transcribed from speech. Ignore the transcript's punctuation and capitalisation; treat fillers and "
        "false starts (\"I... I\", \"uh\") as hesitation, not as content.\n\n"
        f"Rewrite the latest utterance in {lb}. {STYLES.get(style, STYLES['natural'])} Keep the meaning, person "
        f"and tense the learner intended. If the utterance is fully or partly in {la} or another language, "
        f"translate it into {lb} the same way. If it is already correct and natural, return it unchanged "
        "(without the hesitations).\n\n"
        "Output the rewritten utterance only: no quotes, labels or comments."
    )
    if explain:
        text += (f" If you corrected real mistakes, add a line \"{NOTE_SEPARATOR}\" after it, followed by one short "
                 f"sentence in {la} that names the key mistakes.")
    return text


def translate_system(a: str, b: str, mode: str) -> str:
    la, lb = lang_name(a), lang_name(b)
    if mode == "a_to_b":
        rule = f"Translate it into {lb}. If it is already in {lb}, output just \"{NO_TRANSLATION}\"."
    else:
        rule = f"If it is in {lb}, translate it into {la}. Otherwise translate it into {lb}."
    return (
        f"You are a live interpreter. Translate the interlocutor's latest utterance, transcribed from speech. {rule} "
        "Use the conversation for context (topic, names, gender, pronouns). The transcript may contain recognition "
        "errors: translate the most likely intended meaning, keeping the tone and register.\n\n"
        "Output the translation only: no quotes, labels or comments."
    )


def user_message(history: list[str], kind: str, text: str) -> str:
    who = "learner" if kind == "me" else "interlocutor"
    parts = []
    if history:
        parts.append("Conversation so far:\n" + "\n".join(history))
    parts.append(f"Latest utterance ({who}):\n{text}")
    return "\n\n".join(parts)


def history_line(kind: str, text: str) -> str:
    return f"{'Learner' if kind == 'me' else 'Interlocutor'}: {text}"


# --------------------------------------------------------------------------- transcript hygiene

# Phrases Whisper-like models are known to "hear" in silence or music.
_HALLUCINATIONS = re.compile(
    r"субтитр|amara\.org|dimatorzok|продолжение следует|спасибо за просмотр|подписывайтесь на канал|"
    r"thanks? (you )?for watching|subscribe to (my|the|our) channel|like and subscribe",
    re.I,
)


def clean_transcript(text: str, prompt: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not re.search(r"\w", text):
        return ""
    if _HALLUCINATIONS.search(text) and len(text) < 80:
        return ""
    if len(text) > 20 and text.lower() in prompt.lower():   # the model echoed its prompt
        return ""
    return text


def strip_quotes(text: str) -> str:
    text = text.strip()
    pairs = [('"', '"'), ("“", "”"), ("«", "»"), ("'", "'")]
    for left, right in pairs:
        if len(text) > 1 and text.startswith(left) and text.endswith(right):
            return text[1:-1].strip()
    return text
