-- g2b price-match  MySQL 스키마 (표준안)
-- 가정: 단가=나라장터 등록단가 기준, 이미지=우리 스토리지(B), 매칭 재실행은 run_id 이력.

-- 우리 품목 (PK = 사내 품번, nara 의존 안 함)
CREATE TABLE our_product (
  product_id    VARCHAR(40)  PRIMARY KEY,                 -- 사내 품번(surrogate, ours)
  item_idnf_no  VARCHAR(20)  NULL UNIQUE,                 -- 물품식별번호(나라장터 등록분, nullable)
  name          VARCHAR(100) NOT NULL,                    -- 품명 (예: 안내판)
  dtls_prnm_no  VARCHAR(20)  NULL,                        -- 세부품명번호 (매칭 카테고리 좁히기)
  spec_w        INT          NULL,                        -- 규격 가로(mm)
  spec_h        INT          NULL,                        -- 규격 세로(mm)
  spec_t        DECIMAL(6,2) NULL,                        -- 규격 두께(mm)
  material      VARCHAR(40)  NULL,                        -- 재질
  usage_desc    VARCHAR(200) NULL,                        -- 용도 (usage=예약어라 usage_desc)
  model         VARCHAR(60)  NULL,                        -- 모델명
  our_price     INT          NULL,                        -- 우리 단가(나라장터 등록단가 기준)
  created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 경쟁사 매칭 결과 (우리품목 1 : N 경쟁사item), 재실행 이력은 run_id로 구분
CREATE TABLE competitor_match (
  match_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
  product_id    VARCHAR(40)  NOT NULL,                    -- FK → our_product
  run_id        CHAR(36)     NOT NULL,                    -- 매칭 실행 배치 id
  rank_no       TINYINT      NOT NULL,                    -- 유사도 순위
  score         DECIMAL(4,3) NOT NULL,                    -- 유사도 점수(0~1)
  comp_company  VARCHAR(120) NULL,                        -- 경쟁사(계약업체)
  comp_item_idnf VARCHAR(20) NULL,                        -- 경쟁사 물품식별번호
  comp_model    VARCHAR(60)  NULL,
  comp_spec_w   INT NULL, comp_spec_h INT NULL, comp_spec_t DECIMAL(6,2) NULL,
  comp_material VARCHAR(40)  NULL,
  comp_usage    VARCHAR(200) NULL,
  comp_price    INT          NULL,                        -- 경쟁사 계약단가
  contract_type VARCHAR(30)  NULL,                        -- 계약구분(다수공급자계약/제3자단가)
  contract_no   VARCHAR(40)  NULL,
  src_img_url   VARCHAR(300) NULL,                        -- nara 원본 imgSrc(참고)
  created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_product (product_id),
  INDEX idx_run (run_id),
  CONSTRAINT fk_match_product FOREIGN KEY (product_id) REFERENCES our_product(product_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 이미지 (스토리지 B: 우리 스토리지에 저장, 우리 URL로 서빙)
CREATE TABLE product_image (
  image_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
  match_id      BIGINT       NOT NULL,                    -- FK → competitor_match
  storage_key   VARCHAR(200) NOT NULL,                    -- 우리 스토리지 키/경로
  our_url       VARCHAR(300) NOT NULL,                    -- 우리가 서빙하는 URL
  content_type  VARCHAR(40)  DEFAULT 'image/jpeg',
  bytes         INT          NULL,
  fetched_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_img_match FOREIGN KEY (match_id) REFERENCES competitor_match(match_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
