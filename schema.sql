-- ============================================
-- 섹터 브리핑 Supabase 스키마
-- Supabase 대시보드 → SQL Editor 에 붙여넣고 실행
-- ============================================

-- 섹터/거시 스냅샷 (매 실행마다 행 추가)
create table if not exists sector_snapshots (
  id           bigint generated always as identity primary key,
  captured_at  timestamptz not null,
  session_date date        not null,
  market       text        not null,   -- 'US' | 'KR' | 'MACRO'
  category     text        not null,   -- 'sector' | 'macro'
  name         text        not null,   -- '반도체', 'Technology', 'VIX' ...
  ticker       text        not null,
  price        numeric,
  change_pct   numeric,
  sma20        numeric,
  above_sma    boolean,
  slope_up     boolean,
  ext          numeric,                -- 이격도 %
  rs20         numeric,                -- 상대강도(섹터-시장 20일)
  stage        text,                   -- Leading|Healthy|Late-Chase|OK|Improving|Repair|Weak
  status       text                    -- 'Strong'|'OK'|'Watch'|'Avoid'
);

-- 마켓별 요약(로테이션)
create table if not exists briefings (
  id           bigint generated always as identity primary key,
  captured_at  timestamptz not null,
  session_date date        not null,
  market       text        not null,
  above_count  int,
  total_count  int,
  strong_count int,
  ok_count     int,
  watch_count  int,
  avoid_count  int,
  rotation     text                    -- 'RISK-ON'|'SELECTIVE'|'CAUTION'|'RISK-OFF'
);

create index if not exists idx_snap_captured  on sector_snapshots (captured_at desc);
create index if not exists idx_brief_captured on briefings (captured_at desc);

-- ============================================
-- RLS: 공개 대시보드(anon key)에서 읽기만 허용
-- 쓰기는 service_role key(GitHub Actions)가 RLS 우회하므로 정책 불필요
-- ============================================
alter table sector_snapshots enable row level security;
alter table briefings        enable row level security;

drop policy if exists "public read snapshots" on sector_snapshots;
drop policy if exists "public read briefings"  on briefings;

create policy "public read snapshots" on sector_snapshots
  for select using (true);

create policy "public read briefings" on briefings
  for select using (true);
