# Put SANE Studio online with GitHub Pages (free)

This folder is a complete static website. GitHub Pages hosts it for free, and the whole SANE
pipeline runs inside each visitor's browser: people open the link, choose their EEG files, and
download the neutralized files. Nothing is uploaded to GitHub or to any server.

You need a GitHub account. Git is already installed on this PC.

## 1. Create an empty repository on GitHub

1. Sign in at <https://github.com> and click **+** (top right) → **New repository**.
2. **Repository name:** `sane-studio` (any name works; it becomes part of the link).
3. **Visibility: Public.** GitHub Pages is free only for public repositories.
4. Leave **Add a README**, **.gitignore** and **license** unticked, so the repository is empty.
5. Click **Create repository**.

## 2. Send this folder to GitHub

Open **Git Bash** and run these three commands. Replace `YOUR-USERNAME` with your GitHub
username (and `sane-studio` if you chose another name).

```bash
cd "E:/Brain signal work/sane-studio-browser"
git remote add origin https://github.com/YOUR-USERNAME/sane-studio.git
git push -u origin main
```

The first push opens a GitHub sign-in window; sign in there. The upload is about 30 MB.

## 3. Turn on GitHub Pages

1. On the repository page, open **Settings** → **Pages** (left menu).
2. Under **Build and deployment**, set **Source** to **Deploy from a branch**.
3. Set **Branch** to **main** and the folder to **/ (root)**, then click **Save**.
4. Wait 1 to 3 minutes. The **Actions** tab shows a run called *pages build and deployment*;
   when it has a green tick, the link appears at the top of **Settings → Pages**:

   `https://YOUR-USERNAME.github.io/sane-studio/`

## 4. Check that it works

1. Open the link. The project page opens; click **Open SANE Studio** (the app is at
   `.../sane-studio/studio.html`). Under the drop zone a status line says *Preparing the SANE engine*; the first
   visit downloads about 50 MB, then it turns green: *Ready · runs in this browser*.
2. Click a sample dataset, then **Neutralize**, and wait for the results screen.
3. Click **Download** and open the file. Later visits start in a few seconds from the cache.

## 5. Update the site

After changing any file in this folder:

```bash
cd "E:/Brain signal work/sane-studio-browser"
git add -A
git commit -m "Describe the change"
git push
```

The site updates about a minute after the push.

## 6. Take it offline, or bring it back

| What | How |
|---|---|
| Take the site offline | **Settings → Pages**, set **Branch** to **None**, **Save** (or **Unpublish site**) |
| Bring it back | **Settings → Pages**, set **Branch** to **main**, **/ (root)**, **Save** |
| Remove everything | **Settings → General**, bottom of the page: **Delete this repository** |

## Limits worth knowing

- **Browsers:** current Chrome, Edge, Firefox or Safari. Desktop computers are best; phones
  work for small recordings.
- **File size:** everything is processed in the browser's memory. A 114 MB, 31-minute,
  80-channel BDF took about 30 seconds on a laptop. Recordings of several hundred MB can
  exceed what a browser tab allows; for those, use the server version.
- **Sessions:** results stay until the tab is closed or reloaded. Download what you need first.
- **GitHub Pages:** the site must stay under 1 GB (this one is about 30 MB), with a soft limit
  of 100 GB of traffic a month.
- **First visit:** the Python engine (Pyodide, NumPy, SciPy, MNE-Python) loads from the
  jsDelivr CDN, about 50 MB, then comes from the browser cache.
