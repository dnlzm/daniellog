+++
title = 'Hello, Daniellog'
date = 2026-07-20T12:00:00+02:00
draft = false
tags = ['meta']
summary = 'What this blog is, how it is built, and why it is deliberately boring technology.'
+++

Daniellog is a technical-facts blog: AV networking, AVB/Milan, church tech.

It is deliberately built on boring technology:

- Posts are Markdown files in a Git repository
- [Hugo](https://gohugo.io/) turns them into static HTML
- GitHub Actions builds and deploys to GitHub Pages on every push

## Why static HTML?

Because both humans and machines can read it. No JavaScript required to see the content — effective, efficient and safe.

```bash
# publishing a post
git add content/posts/my-new-post.md
git commit -m "post: my new post"
git push   # live in ~1 minute
```

More soon.
