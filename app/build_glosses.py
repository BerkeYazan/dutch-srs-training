"""Removed. The literal_gloss field is now seed-authored.

The Word order drill draws its English-order Dutch glosses from the
`literal_gloss` key inside seed_data.py and from the Custom tab's gloss
input, not from an LLM build pass. This module used to drive a Gemini
reorder call, which was removed because the rest of the deck is hand-paired
and the gloss field belongs in the same authored layer.

If you reach this file from an old shell alias or doc, see:
    [[Methodology]] section 11
    [[SRS/README]] section "Authoring sentences with literal glosses"
"""

raise SystemExit(
    "app/build_glosses.py is removed. literal_gloss is seed-authored, "
    "see Methodology section 11 and SRS/README."
)
