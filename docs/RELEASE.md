# Release: a fresh public repository

This is the maintainer's own procedure. It needs the private working
repository and its private term files, so it cannot be followed from the
public repository.

This is the procedure for publishing a private working repository of this
framework as a new public one, with a single commit and no history. The public
repository starts clean: none of the private history, none of the personal
files, and nothing the scanner or a careful read can find.

Every step runs in Terminal on the Mac that holds the private repository. The
script in step 2 never pushes and never changes the private repository. The push
in step 4 is the one step that leaves the Mac.

## 0. When to run it

Run it only when all of these are true:

- The branch you want to publish is checked out, its working tree is clean, and everything you
  want to publish is committed. The script publishes the committed files of
  `HEAD`, not the files on disk.
- Both scans are clean on that branch:

```bash
./.venv/bin/python3 tools/check_pii.py --tree
./.venv/bin/python3 tools/check_pii.py --tree --private
```

  The first uses the public rules. The second adds the private blocklist
  (`tools/pii_blocklist.txt`) and your own list of words (`user/private_terms.txt`).
  Both files are gitignored and stay on this Mac. The second scan only knows the
  words you put in those files, so add anything you want kept out of the public
  repository before you start: your own and your relatives' names and nicknames,
  places, employers, your own domains, and health details such as medicines.
  Test examples and prompt examples written from real life are where these hide.
- `tools/pii_blocklist.txt` and `user/private_terms.txt` both exist and have
  terms in them. The script refuses to run otherwise, because without them the
  private half of the scan would silently not run. Make the first with
  `make hooks-pii`.
- `git config user.name` and `git config user.email` print the name and address
  you want on the one public commit. The script takes the author from them and
  scans them with the same rules and terms as the files: if they match (your
  private name, or an address that is not a placeholder), it refuses. Set a
  neutral identity for this repository first, or pass the one you want on
  purpose with `--author "Name <email>"`; an explicit `--author` is not scanned.
  The usual choice is your real name with GitHub's no-reply address for your
  account (`<id>+<login>@users.noreply.github.com`, shown under Settings,
  Emails): the commit then counts on your profile and no real inbox is
  published.

## 1. Check the tools you need

```bash
git --version
python3 --version
```

## 2. Build the public tree

Pick a folder that does not exist yet, or is empty:

```bash
bash tools/make_public_tree.sh "$HOME/public-tree"
```

or, with an identity of your choice:

```bash
bash tools/make_public_tree.sh --author "Your Name <email>" "$HOME/public-tree"
```

The script does this, in order:

1. Refuses to run when you give no folder, a folder that is not empty, a folder
   inside the private repository (or inside its main checkout, when you run it
   from a worktree; the check ignores upper and lower case), when either
   private term file is missing or has no terms, when the commit identity
   matches the scan (see step 0), when `HEAD` tracks anything under `.venv/` or
   `.worktrees/`, or when a file name of `HEAD` matches the scan. It never
   deletes anything. It warns when the private repository has uncommitted
   changes, because only `HEAD` is published.
2. Extracts the committed files of `HEAD` into the folder (`git archive`). The
   gitignored `user/` folder, `.env`, `.venv` and the databases are not in a
   commit, so they are not in the folder.
3. Scans the extracted files with the public rules and the private terms.
4. If the scan finds anything, prints `file:line: rule` for each hit (never the
   matching word), leaves the extracted files in place for you to look at, makes
   no `.git`, and exits with code 1. Fix the problem in the private repository,
   commit it, remove the folder yourself, and run the script again.
5. If the scan is clean, starts a new repository in the folder on a branch called
   `main`, adds every file, and makes one commit named `Initial public release`
   with the author from your git settings (or `--author`), dated in UTC. The
   `GIT_AUTHOR_*` and `GIT_COMMITTER_*` variables of your shell are ignored, and
   your global git hooks and ignore rules are not used. The script then checks that the new commit holds exactly
   the same files as `HEAD` of the private repository, and stops with exit code
   1 if it does not.
6. Prints the source branch and commit, how many private terms the scan used
   (a number only), the short commit id, author and date of the new commit, and
   the next step.

Check the result:

```bash
git -C "$HOME/public-tree" log --oneline
ls "$HOME/public-tree"
```

The log shows exactly one commit, and the listing has no `user` folder.

## 3. Read it once more yourself

The scanner finds what it has been told to look for. A person finds the rest.
Before anything is pushed, read the output of these searches in the new tree and
make sure each file name and each hit is a placeholder or a general word. The
first command lists the file names, which the other searches do not read:

```bash
git -C "$HOME/public-tree" ls-files
git -C "$HOME/public-tree" grep -n -I -i -E 'first-name|surname|city|employer|own-domain'
git -C "$HOME/public-tree" grep -n -I -E '@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
git -C "$HOME/public-tree" grep -n -I -E '/(Users|home)/'
```

Replace the words in the second search with your own: your names, your family's
names, your town, your employer and any domain you own. If anything is wrong,
fix it in the private repository, commit, remove the folder you built, and
start again from step 2. Do not edit files inside the built tree: the next build
would not have the fix.

## 3a. Check that the built tree works on its own

The built tree has no `.env` and no `user/`, like a stranger's fresh clone.
Install it and run the tests and the type check there, so the public CI is green
on its first push:

```bash
cd "$HOME/public-tree"
make install
make test
make typecheck
```

Then check the identity on the commit once more. It must be the name and
address you chose, and the date must end in `+0000`:

```bash
git -C "$HOME/public-tree" log -1 --format='%an <%ae> %ad'
```

`make install` makes a `.venv` inside the built tree. It is gitignored, so it is
not pushed; remove it afterwards if you like (`rm -rf "$HOME/public-tree/.venv"`).

## 4. Create the public repository and push (the owner's go)

This is the only step that makes anything public. It waits for your explicit
decision. Nobody, and no agent, does it for you.

1. On GitHub, create a new empty repository, and create it private. Choose the
   owner and name you want to publish under. Do not add a README, a licence or a
   `.gitignore` there: the tree already has them.
2. Add it as the remote of the built tree and push `main`:

```bash
git -C "$HOME/public-tree" remote add origin <url of the new repository>
git -C "$HOME/public-tree" push -u origin main
```

3. Open the new repository (still private) in a browser and read the file
   list and the first commit once, as a stranger would.
4. In the repository settings on GitHub, change it to public. That change is
   the moment the release becomes visible to everyone.
5. Straight after, in the repository settings under Security, turn on "Private
   vulnerability reporting". GitHub offers it only on public repositories, so
   it cannot be turned on earlier. `SECURITY.md` sends people to the "Report a
   vulnerability" button, which only exists once this is on.

The remote of your private working repository is not touched by any of this.

## 5. Bundle the private repository

Keep the full private history as one file. Run this in the private repository,
which stays as it is:

```bash
git bundle create <path>/brain-private-full-<date>.bundle --all
git bundle verify <path>/brain-private-full-<date>.bundle
```

Keep the bundle somewhere safe and never delete it. It is the only copy of the
history that the public repository leaves out.
