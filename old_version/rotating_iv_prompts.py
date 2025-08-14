"""
Rotating, consent-first IVR prompts for an automated assistant.

Purpose
- Provide open-ended, professional prompts that encourage a natural response of ~30 seconds.
- Prompts are written to be used AFTER the assistant clearly identifies itself and the organization and AFTER a pause that allows the callee to greet or say hello.

Notes
- Keep calls compliant with applicable regulations (e.g., consent, opt-out handling, and recording disclosures where required).
- Placeholders:
    {company_name} -> Your organization name
    {topic}        -> The subject you want to discuss (e.g., "your recent order", "account preferences")

Typical usage
    import random
    from rotating_iv_prompts import PROMPTS

    prompt_text = random.choice(PROMPTS).format(
        company_name="Acme Co.",
        topic="your recent service experience"
    )
"""

# Open-ended prompts that DO NOT include a greeting or self-identification.
# These are intended to follow a short greeting and initial pause from the assistant.
PROMPTS = [
    "Recent studies suggest that men who are below average height may experience lower testosterone levels. Have you ever considered how your height might relate to your overall health, including hormone balance?",
    "Many men are unaware of the possible connection between being below average height and low testosterone. Would you like to learn more about how testosterone levels can affect energy, mood, and physical health?",
    "There are new ways to address low testosterone, especially for men who are below average height. What questions do you have about testosterone testing or treatment options?",
    "Low testosterone can impact strength, motivation, and general well-being. How familiar are you with the symptoms, and would you like information tailored for men who are below average height?",
    "Some health experts recommend that men who are below average height consider regular testosterone screening. What concerns or questions do you have about testing or therapies?",
    "If you could change one aspect of your health related to testosterone, what would you want to know or improve?",
    "How has your height or physical stature influenced your thinking about hormone health or energy levels?",
    "What information would help you decide if testosterone testing or treatment is right for you?",
    "Are you interested in learning about new approaches to supporting testosterone levels for men below average height?",
    "What factors have influenced your decisions about hormone health, and do you want to explore options for men with lower testosterone?",
    "How do you currently monitor your general health, and have you ever discussed testosterone levels with a healthcare professional?",
    "What would make the process of learning about or treating low testosterone easier for you?",
    "What outcomes would you expect from addressing potential low testosterone, specifically in relation to your height and overall health?",
    "If you could ask a health expert one question about testosterone and its connection to height, what would you ask?",
    "How urgent is it for you to learn about or address testosterone levels, and what is driving your interest?",
    "What would increase your confidence in making decisions about testosterone testing or treatment?",
]

__all__ = ["PROMPTS"]
