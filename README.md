# medium-export2md

Convert your stories from Medium's official export into clean Markdown for **Hugo** or **Jekyll**, one folder (or file) per post, with front matter and the images downloaded next to the text.

No scraping: Medium gives you the zip itself, and this script only reads it. A single Python file, standard library only (Python 3.9+).

*Versão em português mais abaixo.*

## 1. Get the export

Medium > profile picture > **Settings** > **Security and apps** > **Download your information** > **Export**. When the archive is ready, Medium e-mails you a link to the `.zip`.

The zip also holds personal data (profile, sessions, IP addresses). Keep it out of your site's repository.

## 2. Get the script

```bash
git clone https://github.com/mariorpn/medium-export2md.git
cd medium-export2md
```

## 3. Convert

```bash
# Hugo, writing straight into your site repo
python3 medium-export2md.py ~/Downloads/medium-export.zip -o ~/my-site

# Hugo multilingual (index.pt-br.md / index.en.md, translations in the same bundle)
python3 medium-export2md.py ~/Downloads/medium-export.zip -o ~/my-site --lang-codes pt=pt-br,en=en

# Jekyll
python3 medium-export2md.py ~/Downloads/medium-export.zip -o ~/my-jekyll-site --format jekyll
```

Run with `--dry-run` first to see what would be written. Existing files are never overwritten unless you pass `--force`.

| Option | What it does |
|---|---|
| `--include-drafts` | Also converts unpublished drafts (`draft: true` in Hugo, `_drafts/` in Jekyll) |
| `--lang-codes pt=pt-br,en=en` | Hugo multilingual. Maps detected languages to your site's codes; the first one is the default language |
| `--pair ID:ID` | Marks two posts as translations when they don't link to each other |
| `--tags tags.txt` | Medium's export has no tags. See [`tags.example.txt`](tags.example.txt): lines like `e14af3701206: openshift, logging` (the id is the end of the post URL) |
| `--tz America/Sao_Paulo` | Time zone for dates (default: your machine's) |
| `--section posts` | Hugo content section |
| `--no-images` | Keeps images on Medium's CDN instead of downloading |

## What it handles

- Keeps only your stories; comments/responses you wrote on other posts are skipped.
- Removes the duplicated title, uses your subtitle (or the first sentences) as `description`.
- Code blocks: keeps line breaks, picks the language (Medium's auto-detection labels shell as "lua" or "cpp", so commands are detected as `bash`), drops stray `YAML`/`bash` labels pasted above the code and merges blocks Medium split in two.
- Bold, italic, inline code, links (with Medium tracking parameters removed), lists, quotes, section separators, figures with captions, link cards, YouTube embeds (Hugo shortcode).
- Links between your own posts become internal links (`relref` in Hugo, `post_url` in Jekyll). Posts that link to each other near the top in different languages are treated as translations.
- Escapes what would break the build: Hugo shortcode syntax inside code, Liquid `{{ }}` in Jekyll.

Tested with Hugo 0.167 and the Blowfish theme. The Jekyll output follows kramdown syntax but was checked by reading it, not by a Jekyll build yet.

Looking to convert a single article by its URL instead of your whole export? [Medium2MD](https://github.com/marceloakalopes/medium2md), by Marcelo Lopes, is a different project that does that in the browser.

## License

[MIT](LICENSE)

---

## Português

Converte os seus posts do export oficial do Medium em Markdown limpo para **Hugo** ou **Jekyll**, com front matter e as imagens baixadas junto do texto. Não faz scraping: o próprio Medium entrega o zip e o script só lê esse arquivo. É um único arquivo Python, sem dependências.

1. No Medium: foto do perfil > **Settings** > **Security and apps** > **Download your information** > **Export**. Quando o arquivo fica pronto, chega um e-mail com o link do `.zip`. Ele tem dados pessoais, então não coloque no repositório do site.
2. Baixe o script:

```bash
git clone https://github.com/mariorpn/medium-export2md.git
cd medium-export2md
```

3. Rode, por exemplo, para um site Hugo bilíngue:

```bash
python3 medium-export2md.py ~/Downloads/medium-export.zip -o ~/meu-site \
  --lang-codes pt=pt-br,en=en --tz America/Sao_Paulo --include-drafts --tags tags.txt
```

Use `--dry-run` antes para ver o que seria gravado. Arquivos que já existem só são sobrescritos com `--force`. As opções estão na tabela acima.

O passo a passo completo está no artigo [Do Medium para o GitHub Pages](https://mariorpn.com.br/posts/do-medium-para-o-github-pages/).
