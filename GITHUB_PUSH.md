# GitHub Push Instructions

## After creating a repository on GitHub:

1. **Add remote origin:**
   ```bash
   git remote add origin https://github.com/remardo/nutrios.git
   ```

2. **Push to GitHub:**
   ```bash
   git push -u origin master
   ```

## If you have a different branch name:
```bash
git branch -M main
git push -u origin main
```

## If you need to force push (be careful!):
```bash
git push -u origin master --force
```

## To check current status:
```bash
git status
git log --oneline
```

## Repository URL format:
- HTTPS: `https://github.com/YOUR_USERNAME/REPOSITORY_NAME.git`
- SSH: `git@github.com:YOUR_USERNAME/REPOSITORY_NAME.git`

Replace `YOUR_USERNAME` with your GitHub username and `REPOSITORY_NAME` with your repository name.
