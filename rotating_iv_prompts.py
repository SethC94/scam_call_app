"""
Professional, consent-first IVR prompts for an automated assistant focused on engine help.

Scope
- Focused on automotive engines:
  1) Sourcing and purchasing an engine for a vehicle.
  2) A vehicle’s existing engine requiring replacement.
  3) Post-purchase support or warranty handling for a purchased engine.

Purpose
- Provide open-ended, neutral prompts that encourage natural, detailed responses.
- Prompts are intended to follow a clear, truthful greeting and identification by the caller.

Dialogue pattern
- Each prompt is written as two concise parts, separated by " || " to indicate a pause opportunity:
  Part A: A short, direct question appropriate after initial greeting.
  Part B: A follow-up question to deepen the discussion (kept professional).
  If your application supports it, split on " || " and insert a short pause (e.g., <Pause length='1'/>) before the follow-up.

Notes
- Use only with consent and for lawful purposes. Do not use these prompts to deceive or harass anyone.
- Maintain professional boundaries: no profanity, slurs, threats, personal attacks, or references to protected characteristics.
- Keep content engine-focused and verifiable.
- Placeholders:
    {company_name} -> Your organization name (optional in these prompts)
    {topic}        -> The engine-related subject (e.g., "engine replacement" or "engine help")

Typical usage
    import random
    from rotating_iv_prompts import PROMPTS

    prompt_text = random.choice(PROMPTS).format(
        company_name="Acme Engines",
        topic="engine replacement"
    )
"""

# Engine-help prompts (no self-identification or greeting; those are handled by the caller separately).
# Each string contains two questions separated by " || " to indicate a pause opportunity.
PROMPTS = [
    "I am looking for help with {topic}. Could you assist with sourcing the right engine? || What information would you need from me to get started?",
    "Could you confirm whether you can help with {topic} for my vehicle? || If so, what are the first details you need to check compatibility?",
    "Could you tell me the year, make, and model you need so you can advise on engine options? || Do you also prefer the VIN or the 8th digit for engine code?",
    "What is the best way to confirm fitment for {topic}? || Do you rely on VIN, engine code, or an interchange reference on your end?",
    "Do you prefer new, remanufactured, or used engines for this type of work? || What are the trade-offs you typically consider for each option?",
    "What do you need from me to verify availability for {topic}? || Are there typical lead times I should plan for right now?",
    "Are there common configuration differences I should know about for this vehicle’s engines? || How do you verify compatibility across close model years?",
    "Do you offer installation support for {topic}, or is this parts-only? || If installation is available, what does scheduling usually look like?",
    "What budget range should I consider for parts and labor related to {topic}? || Are there core charges, deposits, or fees I should plan for?",
    "Could you recommend trusted brands or suppliers for this engine? || What distinguishes your preferred option in terms of reliability or support?",
    "What warranty coverage is standard for engines you provide? || Could you summarize coverage terms, exclusions, and any labor considerations?",
    "Are there typical issues with this model’s engines that I should be aware of? || Do you recommend any preventative replacements during the swap?",
    "What is your current workload and earliest availability to help with {topic}? || If lead times vary, what is a realistic scheduling window?",
    "Do you need my location to estimate shipping or logistics for {topic}? || Which shipping options and timeframes are most common for engines?",
    "What is the next step if you can help with {topic}? || Do you prefer to start with a quote, inspection, or parts verification?",
    "Would photos or additional documentation make this easier? || If so, what would be most useful to review first?",
    "How do you handle deposits, invoicing, and scheduling for engine jobs? || Are digital copies of the quote and warranty terms available at order time?",
    "Are ECU or immobilizer considerations relevant for this vehicle? || If reprogramming is needed, what guidance do you typically provide?",
    "Is there a preferred compression ratio or configuration for reliability on this model? || Do you have a recommended parts list to accompany the engine?",
    "What related components do you recommend replacing during the swap? || Are there torque-to-yield fasteners or gaskets that must be new for warranty?",
    "Could you outline what you need to prepare a detailed quote for {topic}? || Do you require the full VIN or just specific identifiers?",
    "Do you provide test evidence for engines you sell, such as compression or leak-down results? || If available, can you share the test date and results format?",
    "If the exact engine is not in stock, do you validate equivalents before proposing an alternate? || How do you confirm compatibility and disclose differences?",
    "Do you offer installation checklists that affect warranty eligibility? || What proof of work do you require to keep coverage valid?",
    "How do you coordinate delivery and receiving for an engine shipment? || Will I receive tracking and a delivery appointment window in advance?",
    "Can you estimate typical turnaround from order to delivery for {topic}? || Are there seasonal or supplier factors that change timelines?",
    "Do you provide a single point of contact throughout the process? || How do you handle updates and technical questions end-to-end?",
    "If issues arise after installation, how do you triage warranty claims? || What evidence would you want submitted up front to expedite review?",
    "Are there emissions or configuration differences I should be aware of for my state? || How do you confirm federal versus CARB configuration in writing?",
    "Do you support verifying sensor and harness compatibility prior to shipment? || What steps do you take to avoid mismatches at install time?",
]

__all__ = ["PROMPTS"]
