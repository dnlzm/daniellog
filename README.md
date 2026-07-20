2. **Add the PaperMod theme as a submodule** (before first push):

   ```bash
   git submodule add --depth=1 https://github.com/adityatelange/hugo-PaperMod.git themes/PaperMod
   git commit -m "add PaperMod theme"
   git push -u origin main
   ```

3. **Enable Pages:** repo → Settings → Pages → Build and deployment →
   Source: **GitHub Actions**. The push in step 2 already triggered the
   workflow; re-run it from the Actions tab if it ran before you enabled Pages.

Site URL: `https://dnlzm.github.io/daniellog/`

## Writing a post

```bash
hugo new content/posts/2026-08-01-my-topic.md   # or just copy an existing post
# edit in VS Code, set draft = false
git add . && git commit -m "post: my topic" && git push
```

Live in about a minute. Local preview: `hugo server -D` → http://localhost:1313

## Custom domain (later)

Settings → Pages → Custom domain. Add a `static/CNAME` file containing the
domain, set `baseURL` in `hugo.toml` accordingly, and update the URLs in
`robots.txt` / `llms.txt`. Nothing else changes.

## AI-visibility checklist (already wired in)

- `static/robots.txt` — explicit allow for GPTBot, ClaudeBot, PerplexityBot,
  Google-Extended, OAI-SearchBot, Applebot-Extended and others
- `static/llms.txt` — points AI systems at the post index and sitemap
- Sitemap + RSS generated automatically by Hugo
- Pure static HTML, no JS needed to read content
