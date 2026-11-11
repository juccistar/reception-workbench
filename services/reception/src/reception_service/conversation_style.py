"""Small social-intent hints; live reply wording remains the AI's responsibility."""
from __future__ import annotations
import re

# Whole-clause recognition only: a greeting followed by an actual request must
# still take the normal procurement path. Unknown chat remains available to AI.
_SOCIAL = {
    'greeting': re.compile(r'(?:hi|hello|hey)(?: there| team| friend| dear)?|greetings|good (?:morning|afternoon|evening)|are you (?:there|online)|is anyone (?:there|available)|你好|您好|嗨|哈喽|早上好|上午好|下午好|晚上好|早安|在吗|你在吗|有人吗|bonjour|salut|bonsoir|hola|buenos días|buenas tardes', re.I),
    'wellbeing': re.compile(r'how (?:are you(?: doing)?|is your day(?: going)?|are things)|how[\x27’]s (?:it going|your day)|(?:i am|i[\x27’]m) (?:fine|good|great|well|doing well)(?: too)?|hope you(?:[\x27’]re| are) (?:well|doing well)|你好吗|您好吗|最近怎么样|最近还好吗|我很好|挺好的|ça va|comment (?:allez-vous|ça va)', re.I),
    'thanks': re.compile(r'thank(?:s| you)(?: so much| very much| a lot)?(?: for (?:the |your )?(?:help|information|details|update|assistance))?|many thanks|you[\x27’]re welcome|谢谢(?:您|你)?|多谢|感谢(?:您|你)?|谢谢(?:您|你)?的帮助|不客气|merci(?: beaucoup)?|gracias', re.I),
    'ack': re.compile(r'ok(?:ay)?|got it|understood|noted|received|sounds good|all right|sure|好的?|明白(?:了)?|了解(?:了)?|收到|没问题|👍|🙏|👌', re.I),
    'wishes': re.compile(r'have a (?:great|good|nice|lovely|wonderful) (?:day|weekend|week|evening)|happy (?:new year(?: 20\d\d)?|holidays|birthday)|you too|祝(?:你|您)?(?:周末愉快|新年快乐|节日快乐|工作顺利)|周末愉快|新年快乐|节日快乐|同乐|bonne journée|bon week-end', re.I),
    'farewell': re.compile(r'bye(?: bye)?|goodbye|see you(?: later| soon)?|take care|再见|拜拜|回头聊|au revoir|adiós', re.I),
}


def social_intent(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 1000:
        return None
    clauses = [part.strip(' \t\r\n:：~～—-') for part in re.split(r'[,.!?;，。！？；\n]+', text) if part.strip()]
    kinds = []
    for clause in clauses:
        kind = next((key for key, pattern in _SOCIAL.items() if pattern.fullmatch(clause)), None)
        if kind is None:
            return None
        kinds.append(kind)
    return next((key for key in ('wellbeing', 'greeting', 'wishes', 'thanks', 'farewell', 'ack') if key in kinds), None)


def demo_social_reply(text, intent):
    """Demo-only examples; production uses the authorized AI integration."""
    chinese = bool(re.search(r'[一-鿿]', text))
    replies = {
        'greeting': ('您好，我在。有什么可以帮您？', "Hello! I'm here and happy to help. How can I help you?"),
        'wellbeing': ('谢谢关心，我在这里为您提供帮助。您今天过得怎么样？', "I'm here and ready to help, thanks for asking! How are you?"),
        'thanks': ('不客气，很高兴能帮到您。', "You're welcome! Happy to help."),
        'ack': ('好的，收到。有其他问题随时告诉我。', 'Got it, thank you. Let me know if you need anything else.'),
        'wishes': ('谢谢，也祝您一切顺利！', 'Thank you! Wishing you a lovely day too.'),
        'farewell': ('再见，有需要随时联系我。', 'Take care! Feel free to message me if you need help.'),
    }
    return replies[intent][0 if chinese else 1]
