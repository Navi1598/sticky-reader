# Sticky Reader

Every morning, this project picks one article per topic, splits it into
pages sized for the Seeed reTerminal Sticky (480x800 portrait), and publishes
them as web pages. Each SenseCraft design shows one page, and you read by pressing
the **down** button on the Sticky.

All articles share **one pool of pages** (default **28**, `p01.html` ... `p28.html`).
Articles are placed one after another with no blank gaps: if today's technology
article needs 5 pages it takes p01–p05, and politics starts on p06. The top of
each page shows the topic and "page 2/6" for that article. Pages left over at the
end say "That's all for today". Only if the day's articles don't all fit is the
last one cut short.

A typical article from The Conversation (800–1,300 words) takes 5–7 pages, so
4 topics usually need 22–28 pages.

---

## Part 1 – Put it on GitHub (about 10 min)

1. **Create a repository.** On github.com click **+ → New repository**.
   Name it `sticky-reader`, set it to **Public** (free GitHub Pages needs a public repo),
   and click **Create repository**.
2. **Upload the files.** On the new repo page click **uploading an existing file**.
   Unzip `sticky-reader.zip` on your computer and drag **everything inside the folder**
   (including the `.github` folder) into the browser. Click **Commit changes**.
   - If the `.github` folder doesn't upload (hidden folders sometimes get skipped),
     click **Add file → Create new file**, type the name
     `.github/workflows/daily.yml`, paste the contents of that file, and commit.
3. **Let the workflow save files.** Go to **Settings → Actions → General →
   Workflow permissions**, choose **Read and write permissions**, and click **Save**.
4. **Turn on GitHub Pages.** Go to **Settings → Pages**. Under *Build and deployment*,
   set **Source: Deploy from a branch**, **Branch: main**, **Folder: /docs**, and click **Save**.
5. **Run it once now.** Go to **Actions → Build daily Sticky pages → Run workflow**.
   Wait about a minute until it shows a green check.
6. **Check the pages.** Open `https://YOUR-USERNAME.github.io/sticky-reader/`.
   You'll see a list of all pages and a preview of each. (The first time,
   Pages can take a few minutes to go live.)

After that, it runs automatically every day around 5:41 AM Eastern.

---

## Part 2 – Set up the designs in SenseCraft (one time, about 20 min)

Do this for each page, p01 through p28 (or up to your `total_pages`):

1. In SenseCraft HMI go to **Workspace → Design** and create a new design.
   Name it with its number so they sort in order: `01`, `02`, ... `28`.
   (Topics move around from day to day, so name designs by number, not topic.)
2. Add the **HTML/Web element** that takes a URL and stretch it to cover the
   **whole canvas** (480x800).
3. Set its URL to `https://YOUR-USERNAME.github.io/sticky-reader/p01.html`
   (`p02.html` for the next design, and so on).
4. Save.

**Tip:** build **p01 first**, check it on the Sticky, then duplicate it and
change only the name and the URL number.

On the Sticky, open **Designs**, tap **01**, then press **down** to read through all of them.

**Already have 16 designs from the first version?** Keep them as they are (they
still point at p01–p16) and add 12 more for p17–p28. Rename the old ones `01`–`16`
if they have topic names.

---

## Adjusting it

Everything is in `config.yaml`. Edit it on GitHub (pencil icon) and commit:

- **Text cut off at the bottom of a page?** Lower `lines_per_page` (e.g. 23 → 21).
- **Too much empty space at the bottom?** Raise `lines_per_page` by 1.
- **Last article often cut short ("Continues online")?** Raise `total_pages`
  (e.g. 28 → 32) and add matching designs (p29–p32).
- **Fewer designs?** Lower `total_pages`. It must match the number of designs you have.
- **Different topics or sources?** Edit the `topics` list. Any RSS/Atom feed works;
  `keywords` limits a topic to matching articles.

Articles already shown are remembered in `docs/state.json`, so you won't get
repeats. If a topic has nothing new, its pages say so and you can skip ahead.

## Sources used

All topics come from **The Conversation**, whose articles are free and published
under a Creative Commons license (CC BY-ND) that allows republishing with credit.
Each article shows its author and source, and the last page links the original.
Battery stories are pulled from several sections by keyword, so on some days
there may be nothing new for that topic.

**Why not Electrek, Ars Technica, New Scientist, etc.?** GitHub Pages sites are
public, so putting another publisher's full articles there would be republishing
their copyrighted work. Only add sources whose license allows it (Creative Commons,
public domain such as NASA), or keep those for headline-only RSS pages in SenseCraft.
The pages are marked `noindex` so search engines skip them.
