# generate-passphrase

Drop-in Ansible task file that generates a strong xkcd-style passphrase
with a random digit injected. Output lands in the fact `passphrase`.

## Shape

`Word7+Word+Word3+Word` — four capitalised dictionary words, joined by
a random special character (`^ - = + _`), with one of those words
suffixed with a random digit. Length is retried until ≥ 29 characters
so it satisfies most "min 12" / "min 16" / "min 28" password policies.

## Dependencies

- `xkcdpass` Python package on the **control node** (auto-installed via
  pip on first run).
- `community.general` collection (for the `random_words` lookup).

## Usage

```yaml
- name: Make a passphrase
  block:
    - ansible.builtin.include_tasks: generate_passphrase.yml
  delegate_to: localhost
  become: false

- name: Use it
  ansible.builtin.debug:
    msg: "Generated: {{ passphrase }}"
```

Wrap the include in a `delegate_to: localhost` block when the calling
play targets a remote host — `xkcdpass` runs on the control node, and
remote hosts may not have it installed.

Do **not** use `delegate_facts: true`. We want the resulting
`passphrase` fact on the *calling* host's vars (so subsequent tasks on
that host can reference it), not on localhost.

## Why not just `lookup('password', ...)` or `random_string`?

Both work, but produce either flat random characters (hard to type,
hard to remember) or a single passphrase word with no policy controls.
This pattern gives you:

- multiple capitalised dictionary words (memorable, dictation-friendly)
- a special character delimiter (covers complexity rules)
- one digit (covers digit-required rules)
- a length floor enforced by retry

…in one fact, with `no_log` discipline so the value never lands in
playbook output.
