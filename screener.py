name: StockPilot KR Screener
on:
  schedule:
    # 08:00~08:30 KST = 23:00~23:30 UTC (일~목)
    - cron: '0,30 23 * * 0-4'
    # 09:00~15:30 KST = 00:00~06:30 UTC (월~금)
    - cron: '0,30 0-6 * * 1-5'
  workflow_dispatch:
permissions:
  contents: write
jobs:
  run-screener:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          # PAT로 푸시해야 봇 커밋이 '사람 활동'으로 잡혀 스케줄 60일 자동중단을 막음 (없으면 기존 토큰 폴백)
          token: ${{ secrets.PAT || github.token }}
          persist-credentials: true
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: pip install requests pandas finance-datareader yfinance beautifulsoup4 pykrx numpy
      - name: Run screener
        env:
          KIS_APP_KEY:     ${{ secrets.KIS_APP_KEY }}
          KIS_APP_SECRET:  ${{ secrets.KIS_APP_SECRET }}
          KIS_ACCOUNT_NO:  ${{ secrets.KIS_ACCOUNT_NO }}
          DISCORD_WEBHOOK: ${{ secrets.DISCORD_WEBHOOK }}
        run: python screener.py
      - name: Run expiry analysis
        run: python expiry.py
      - name: Run trader
        env:
          KIS_APP_KEY_MOCK:    ${{ secrets.KIS_APP_KEY_MOCK }}
          KIS_APP_SECRET_MOCK: ${{ secrets.KIS_APP_SECRET_MOCK }}
          KIS_ACCOUNT_MOCK:    ${{ secrets.KIS_ACCOUNT_MOCK }}
          DISCORD_WEBHOOK:     ${{ secrets.DISCORD_WEBHOOK }}
          KIS_MOCK:            "true"
        run: python trader.py
      - name: Commit results
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          mkdir -p /tmp/out
          for f in results.json positions.json expiry_result.json market_history.json candidates_cache.json regime_cache.json defense_state.json; do
            test -f "$f" && cp "$f" "/tmp/out/$f"
          done
          git fetch origin main
          git reset --hard origin/main
          for f in results.json positions.json expiry_result.json market_history.json candidates_cache.json regime_cache.json defense_state.json; do
            test -f "/tmp/out/$f" && cp "/tmp/out/$f" "$f" && git add "$f"
          done
          git diff --cached --quiet || git commit -m "auto: screener+trader $(date +'%Y%m%d %H%M')"
          git push
