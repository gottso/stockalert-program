# 섹터 로테이션 브리핑

미국(SPDR 11섹터) + 한국(섹터 ETF) + 거시지표를 매일 2회 수집해서
20SMA 기준 상태(Strong/OK/Watch/Avoid)를 계산하고,
**텔레그램 발송 + 웹 대시보드**로 보여주는 시스템.

## 상태 로직 (2×2)
| 20SMA | Slope | Status |
|-------|-------|--------|
| Above | Up    | 🟢 Strong |
| Above | Down  | 🟦 OK |
| Below | Up    | 🟧 Watch |
| Below | Down  | 🔴 Avoid |

로테이션: 20SMA 위 비율 → 75%↑ RISK-ON / 50%↑ SELECTIVE / 25%↑ CAUTION / 그 외 RISK-OFF

---

## 설정 순서

### 1) 텔레그램 봇
1. `@BotFather` → `/newbot` → 이름/username(끝이 `bot`) 입력 → **토큰** 받기
2. 만든 봇과 대화 시작 → 아무 메시지 전송
3. `https://api.telegram.org/bot<토큰>/getUpdates` 접속 → `chat.id` 숫자 확인

### 2) Supabase
1. supabase.com → New Project 생성
2. **SQL Editor** 에 `schema.sql` 전체 붙여넣고 실행
3. Settings → API 에서 3개 값 복사:
   - `Project URL`
   - `anon public` 키 → 대시보드용
   - `service_role` 키 → 수집기(쓰기)용 ⚠️ 비공개

### 3) GitHub
1. 이 폴더 전체를 새 repo에 업로드 (`.github/workflows/briefing.yml` 포함)
2. repo → Settings → Secrets and variables → Actions → New secret 로 4개 등록:
   | 이름 | 값 |
   |------|----|
   | `TELEGRAM_TOKEN` | 봇 토큰 |
   | `TELEGRAM_CHAT_ID` | chat id |
   | `SUPABASE_URL` | Project URL |
   | `SUPABASE_SERVICE_KEY` | service_role 키 |
3. Actions 탭 → `sector-briefing` → **Run workflow** 로 즉시 테스트

### 4) 웹 대시보드 (GitHub Pages)
1. `index.html` 상단 두 줄을 본인 값으로 교체:
   ```js
   const SUPABASE_URL  = "https://xxxx.supabase.co";
   const SUPABASE_ANON = "eyJhbGciOi...";  // anon key
   ```
2. repo → Settings → Pages → Branch `main` / root 선택
3. `https://<아이디>.github.io/<repo>/` 접속

---

## 실행 시간 (KST)
- **16:00** 한국장 마감 후
- **06:30** 미국장 마감 후
- (GitHub Actions 스케줄은 UTC 기준이며, 서버 부하 시 수 분 지연될 수 있음)

## 종목 수정
`collect_and_brief.py` 상단 `US_SECTORS` / `KR_SECTORS` / `MACRO` 딕셔너리만 편집.
잘못된 티커는 자동으로 건너뛰고 로그에 `[WARN]` 남김.
