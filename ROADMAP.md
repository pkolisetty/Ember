# Roadmap

- [ ] Kinship terms: answer "what do I call X?" from the family tree (e.g. Telugu athayya / pinni / mavayya / babai), using each person's gender and whether they're older or younger than the connecting parent.
- [ ] Store gender and birth year for each person.
- [ ] `merge_people` tool and a review flow for people flagged as uncertain matches.
- [ ] Encrypted offsite backup of `raw/` and the people/relations files.
- [ ] Anthropic API key as an alternative to `claude -p`.
- [ ] A periodic worker (launchd/systemd) that retries failed entries without opening Claude.
- [ ] Log entries about past days with the correct date while still resolving "yesterday" correctly.
- [ ] Link entries to larger documents (decision research, receipts).
- [ ] Weekly digest, "haven't seen X in N months" nudges, birthdays, trend dashboard (mood, spending).
- [ ] Optional fully local mode (Ollama for extraction).
