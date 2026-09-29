# Telegram bet drafts

A linked private chat has at most one active draft. A photo opens a draft in `AWAITING_EXTRACTION`; the photo file reference is encrypted. The image-reading worker in issue #100 will call `apply_extraction` with extracted fields, source confidence, and a media hash. Until that reader is available, the bot acknowledges the photo and keeps the draft pending. Sending another photo while a draft is active prompts the person to use `/continuar` or `/cancelar` and does not replace the first photo.

`/continuar` reloads the same draft after a restart. The person can supply one or several fields, such as `casa=Betano; stake=2`, or use `/corrigir odd 1,90`. Corrections change only the named fields and append a versioned correction record. The summary shows values already understood and requests only missing or low-confidence fields; it never asks for the photo again. `/cancelar` closes the draft and permits a new photo.

The account resolver uses the bet's `data_aposta`, not ingestion time. When exactly one account matches that house and instant, its ID is attached automatically. When none or several match, the draft asks for the account. An explicit account is checked against the tenant and house. The resolver returns `NONE`, `UNIQUE`, or `AMBIGUOUS` and remains ready for future simultaneous accounts.

`rascunhos_aposta` and `rascunho_correcoes` have tenant RLS, audit triggers, and no path into `apostas` or financial views. `AWAITING_CONFIRMATION` means the fields are complete; confirmation and financial creation belong to issue #101. Both draft tables are included in account export and anonymization. Never include photo IDs or extracted text in logs.
