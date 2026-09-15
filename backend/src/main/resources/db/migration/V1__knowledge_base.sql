-- Design DDL v1.0, MySQL 8.4. Not executed or an applied migration.
-- No database creation/drop, secrets, seed accounts, or company content.
-- UTC DATETIME(6); application must set connection time zone to +00:00.
-- Polymorphic job.target_id and audit object ids are checked by services.

CREATE TABLE tenant (
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '租户唯一标识符 (UUID)',
  code VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '租户唯一业务编码 (如企业代码/租户英文标识)',
  name VARCHAR(128) NOT NULL COMMENT '租户显示名称',
  security_epoch BIGINT NOT NULL DEFAULT 0 COMMENT '租户安全纪元版本号，权限或安全策略变更时递增以使旧缓存失效',
  active_retrieval_config_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '租户当前全局默认生效的检索配置版本ID',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '租户记录创建时间戳 (UTC)',
  PRIMARY KEY (id),
  UNIQUE KEY uq_tenant_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='租户信息表，存储系统多租户隔离根实体及租户全局配置状态';

CREATE TABLE app_user (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '用户唯一标识符 (UUID)',
  login_name VARCHAR(128) NOT NULL COMMENT '用户登录用户名 (租户内唯一)',
  password_hash VARCHAR(255) NOT NULL COMMENT '用户密码哈希值 (BCrypt加密)',
  is_admin BOOLEAN NOT NULL DEFAULT FALSE COMMENT '是否为租户管理员标识',
  status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE' COMMENT '账号生命周期状态 (ACTIVE: 正常, DISABLED: 已禁用)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '用户账号创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '用户账号更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_user_login (tenant_id, login_name),
  CHECK (status IN ('ACTIVE','DISABLED')),
  CONSTRAINT fk_1 FOREIGN KEY (tenant_id) REFERENCES tenant (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='租户用户表，存储租户下属用户信息与认证凭证';

CREATE TABLE auth_session (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '会话唯一标识符 (UUID)',
  user_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属用户唯一标识符',
  token_hash BINARY(32) NOT NULL COMMENT '会话认证令牌的 SHA-256 二进制哈希值 (32字节)',
  expires_at DATETIME(6) NOT NULL COMMENT '会话过期失效时间戳 (UTC)',
  revoked_at DATETIME(6) NULL COMMENT '会话被主动吊销/登出的时间戳 (UTC, NULL表示有效)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '会话创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '会话更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_session_token (token_hash),
  KEY ix_session_user (tenant_id,user_id,expires_at),
  CONSTRAINT fk_2 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_3 FOREIGN KEY (tenant_id,user_id) REFERENCES app_user (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户认证会话表，维护已登录用户的会话状态与 Token 哈希';

CREATE TABLE user_group (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '用户组唯一标识符 (UUID)',
  name VARCHAR(128) NOT NULL COMMENT '用户组显示名称 (租户内唯一)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '用户组创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '用户组更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_group_name (tenant_id,name),
  CONSTRAINT fk_4 FOREIGN KEY (tenant_id) REFERENCES tenant (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户组表，维护租户内用于权限批量授权的用户组定义';

CREATE TABLE group_member (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '成员关联记录唯一标识符 (UUID)',
  group_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属用户组标识符',
  user_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '关联的用户标识符',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '成员加入用户组时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '成员关联更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_member (tenant_id,group_id,user_id),
  KEY ix_member_user (tenant_id,user_id),
  CONSTRAINT fk_5 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_6 FOREIGN KEY (tenant_id,group_id) REFERENCES user_group (tenant_id,id),
  CONSTRAINT fk_7 FOREIGN KEY (tenant_id,user_id) REFERENCES app_user (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户组成员关联表，定义用户与用户组的多对多归属关系';

CREATE TABLE knowledge_base (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '知识库唯一标识符 (UUID)',
  name VARCHAR(255) NOT NULL COMMENT '知识库显示名称',
  status VARCHAR(16) NOT NULL DEFAULT 'LIVE' COMMENT '知识库可用状态 (LIVE: 正常可用, DISABLED: 已停用)',
  row_version BIGINT NOT NULL DEFAULT 0 COMMENT '乐观锁行版本号',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '知识库创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '知识库更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  CHECK (status IN ('LIVE','DISABLED')),
  CONSTRAINT fk_8 FOREIGN KEY (tenant_id) REFERENCES tenant (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='知识库表，表示租户下独立隔离的知识集合与权限边界';

CREATE TABLE kb_grant (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '授权记录唯一标识符 (UUID)',
  kb_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '被授权访问的目标知识库标识符',
  principal_type VARCHAR(8) NOT NULL COMMENT '授权主体类型 (USER: 用户, GROUP: 用户组, ALL: 全员)',
  user_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '被授权的用户标识符 (当 principal_type 为 USER 时非空)',
  group_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '被授权的用户组标识符 (当 principal_type 为 GROUP 时非空)',
  principal_key VARCHAR(80) CHARACTER SET ascii COLLATE ascii_bin GENERATED ALWAYS AS (CONCAT(principal_type,':',COALESCE(user_id,group_id,'*'))) STORED COMMENT '主体唯一复合键 (虚拟存储生成列, 格式: TYPE:ID/*)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '授权规则创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '授权规则更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_kb_grant (tenant_id,kb_id,principal_key),
  CHECK ((principal_type='USER' AND user_id IS NOT NULL AND group_id IS NULL) OR (principal_type='GROUP' AND group_id IS NOT NULL AND user_id IS NULL) OR (principal_type='ALL' AND user_id IS NULL AND group_id IS NULL)),
  CONSTRAINT fk_9 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_10 FOREIGN KEY (tenant_id,kb_id) REFERENCES knowledge_base (tenant_id,id),
  CONSTRAINT fk_11 FOREIGN KEY (tenant_id,user_id) REFERENCES app_user (tenant_id,id),
  CONSTRAINT fk_12 FOREIGN KEY (tenant_id,group_id) REFERENCES user_group (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='知识库访问控制授权表 (KB ACL)，定义用户或用户组对知识库的访问权限';

CREATE TABLE document (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '文档唯一标识符 (UUID)',
  kb_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属知识库标识符',
  title VARCHAR(512) NULL COMMENT '文档标题/显示名称',
  acl_mode VARCHAR(16) NOT NULL DEFAULT 'RESTRICT' COMMENT '访问控制继承模式 (INHERIT: 继承所属知识库权限, RESTRICT: 自定义文档独立权限)',
  status VARCHAR(20) NOT NULL DEFAULT 'LIVE' COMMENT '文档生命周期状态 (LIVE: 正常, TRASHED: 回收站, PURGE_PENDING: 待物理清除, PURGED: 已清除)',
  active_build_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '当前处于生效/发布状态的索引构建版本ID (外键延后定义)',
  lifecycle_epoch BIGINT NOT NULL DEFAULT 0 COMMENT '文档生命周期代际纪元号，用于版本失效与并发控制',
  row_version BIGINT NOT NULL DEFAULT 0 COMMENT '乐观锁行版本号',
  deleted_at DATETIME(6) NULL COMMENT '放入回收站的时间戳 (UTC, NULL表示未删除)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '文档创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '文档更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  KEY ix_doc_kb (tenant_id,kb_id,status),
  CHECK (acl_mode IN ('INHERIT','RESTRICT')),
  CHECK (status IN ('LIVE','TRASHED','PURGE_PENDING','PURGED')),
  CONSTRAINT fk_13 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_14 FOREIGN KEY (tenant_id,kb_id) REFERENCES knowledge_base (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='知识文档主表，维护文档元数据、生命周期状态与权限模式';

CREATE TABLE document_grant (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '授权记录唯一标识符 (UUID)',
  document_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '被授权访问的目标文档标识符',
  principal_type VARCHAR(8) NOT NULL COMMENT '授权主体类型 (USER: 用户, GROUP: 用户组)',
  user_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '被授权的用户标识符 (当 principal_type 为 USER 时非空)',
  group_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '被授权的用户组标识符 (当 principal_type 为 GROUP 时非空)',
  principal_key VARCHAR(80) CHARACTER SET ascii COLLATE ascii_bin GENERATED ALWAYS AS (CONCAT(principal_type,':',COALESCE(user_id,group_id,'*'))) STORED COMMENT '主体唯一复合键 (虚拟存储生成列, 格式: TYPE:ID)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '授权规则创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '授权规则更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_document_grant (tenant_id,document_id,principal_key),
  CHECK ((principal_type='USER' AND user_id IS NOT NULL AND group_id IS NULL) OR (principal_type='GROUP' AND group_id IS NOT NULL AND user_id IS NULL)),
  CONSTRAINT fk_15 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_16 FOREIGN KEY (tenant_id,document_id) REFERENCES document (tenant_id,id),
  CONSTRAINT fk_17 FOREIGN KEY (tenant_id,user_id) REFERENCES app_user (tenant_id,id),
  CONSTRAINT fk_18 FOREIGN KEY (tenant_id,group_id) REFERENCES user_group (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='文档独立访问控制授权表 (Doc ACL)，定义文档在 RESTRICT 模式下的访问主体';

CREATE TABLE artifact (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '工件唯一标识符 (UUID)',
  document_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属文档标识符',
  kind VARCHAR(32) NOT NULL COMMENT '工件业务类型 (如 ORIGINAL_FILE: 原始文件, PARSED_JSON: 解析结构体)',
  storage_key VARCHAR(512) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '对象存储或底层文件系统中的唯一路径标识键',
  sha256 BINARY(32) NOT NULL COMMENT '二进制工件内容的 SHA-256 哈希校验值 (32字节)',
  size_bytes BIGINT NOT NULL COMMENT '工件文件实际字节大小',
  status VARCHAR(16) NOT NULL DEFAULT 'READY' COMMENT '工件就绪状态 (如 READY: 就绪可用)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '工件上传/创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '工件更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_artifact_key (storage_key),
  UNIQUE KEY uq_artifact_doc (tenant_id,document_id,id),
  CHECK (size_bytes >= 0),
  CONSTRAINT fk_19 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_20 FOREIGN KEY (tenant_id,document_id) REFERENCES document (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='文档工件表，持久化存储文档的原始上传文件与衍生二进制文件元数据';

CREATE TABLE document_version (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '版本唯一标识符 (UUID)',
  document_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属文档标识符',
  version_no INT NOT NULL COMMENT '文档单调递增版本序号 (从 1 开始)',
  source_artifact_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '对应原始上传源文件的工件标识符',
  source_hash BINARY(32) NOT NULL COMMENT '源文件内容 SHA-256 哈希校验指纹 (32字节)',
  original_name VARCHAR(512) NULL COMMENT '上传时的源文件原始名称',
  media_type VARCHAR(128) NOT NULL COMMENT '源文件 MIME 媒体类型 (如 application/pdf)',
  created_by CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '版本上传人用户标识符',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '版本创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '版本更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_doc_version (tenant_id,document_id,version_no),
  UNIQUE KEY uq_version_doc (tenant_id,document_id,id),
  CHECK (version_no > 0),
  CONSTRAINT fk_21 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_22 FOREIGN KEY (tenant_id,document_id) REFERENCES document (tenant_id,id),
  CONSTRAINT fk_23 FOREIGN KEY (tenant_id,document_id,source_artifact_id) REFERENCES artifact (tenant_id,document_id,id),
  CONSTRAINT fk_24 FOREIGN KEY (tenant_id,created_by) REFERENCES app_user (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='文档版本历史表，追踪记录文档源文件的每一次变更与版本快照';

CREATE TABLE config_revision (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '配置版本唯一标识符 (UUID)',
  kind VARCHAR(24) NOT NULL COMMENT '配置类别 (BUILD: 索引构建, RETRIEVAL: 检索参数, MODEL: 模型网关, PROMPT: 提示词模板, LIMITS: 限流与配额, EVAL: 评测配置)',
  name VARCHAR(128) NOT NULL COMMENT '策略配置标识名称',
  revision INT NOT NULL COMMENT '配置单调递增修订版本号 (从 1 开始)',
  sha256 BINARY(32) NOT NULL COMMENT '配置负载 JSON 的 SHA-256 防篡改校验哈希 (32字节)',
  payload JSON NOT NULL COMMENT '配置参数具体内容的 JSON 结构体',
  status VARCHAR(16) NOT NULL DEFAULT 'DRAFT' COMMENT '配置生效状态 (DRAFT: 草稿, ACTIVE: 生效中, ARCHIVED: 已归档)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '配置修订版本创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '配置修订版本更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_config_revision (tenant_id,kind,name,revision),
  CHECK (revision > 0),
  CHECK (kind IN ('BUILD','RETRIEVAL','MODEL','PROMPT','LIMITS','EVAL')),
  CONSTRAINT fk_25 FOREIGN KEY (tenant_id) REFERENCES tenant (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='系统策略配置版本表，统一管理切片、检索、模型、Prompt等核心策略的版本化快照';

CREATE TABLE index_build (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '构建版本唯一标识符 (UUID)',
  document_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '目标文档标识符',
  version_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '目标文档版本标识符',
  build_no INT NOT NULL COMMENT '文档构建单调递增序号 (从 1 开始)',
  config_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '构建所采用的 BUILD 类别配置版本标识符',
  status VARCHAR(24) NOT NULL DEFAULT 'QUEUED' COMMENT '构建状态 (QUEUED: 排队中, RUNNING: 处理中, READY: 已完成可用, FAILED: 失败)',
  model_fingerprint VARCHAR(255) NULL COMMENT '向量化所用 Embedding 模型的指纹标识',
  embedding_dims INT NULL COMMENT '向量特征维度 (例如 1536)',
  capabilities JSON NOT NULL COMMENT '索引能力特性的 JSON 描述 (如支持稠密向量、全文检索等)',
  manifest_hash BINARY(32) NULL COMMENT '索引切片元数据清单的 SHA-256 哈希值 (32字节)',
  fence BIGINT NOT NULL COMMENT '隔离栅栏计数器，用于并发状态机CAS更新与防脑裂',
  published_at DATETIME(6) NULL COMMENT '索引构建正式对外发布生效的时间戳 (UTC, NULL表示未发布)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '构建记录创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '构建记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_build_no (tenant_id,version_id,build_no),
  UNIQUE KEY uq_build_doc (tenant_id,document_id,id),
  CHECK (build_no > 0),
  CHECK (embedding_dims IS NULL OR embedding_dims > 0),
  CONSTRAINT fk_26 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_27 FOREIGN KEY (tenant_id,document_id,version_id) REFERENCES document_version (tenant_id,document_id,id),
  CONSTRAINT fk_28 FOREIGN KEY (tenant_id,config_id) REFERENCES config_revision (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='文档索引构建版本表，记录每次文档切片、Embedding 向量化建库的执行结果与发布版本';

CREATE TABLE source_unit (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '源单元唯一标识符 (UUID)',
  build_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属索引构建标识符',
  unit_key VARCHAR(191) NOT NULL COMMENT '构建版本内源单元唯一键 (如 page_1_block_2)',
  kind VARCHAR(16) NOT NULL COMMENT '源单元版面类型 (TEXT: 纯文本, CODE: 代码块, TABLE: 表格, IMAGE: 图像, HEADING: 标题层级)',
  text_content LONGTEXT NULL COMMENT '单元的文本或转义正文内容',
  location JSON NOT NULL COMMENT '版面物理位置信息的 JSON 描述 (如页码、坐标框 bbox、行号等)',
  machine_generated BOOLEAN NOT NULL DEFAULT FALSE COMMENT '是否为多模态/OCR/大模型自动生成的补充说明标识',
  artifact_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '关联的二进制衍生工件标识符 (如提取的图像截图)',
  content_hash BINARY(32) NOT NULL COMMENT '单元正文内容 SHA-256 哈希值 (32字节)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '源单元创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '源单元更新时间戳 (UTC)',
  document_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属文档标识符 (用于复合外键层级约束)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_unit_key (tenant_id,build_id,unit_key),
  UNIQUE KEY uq_unit_build (tenant_id,build_id,id),
  CHECK (kind IN ('TEXT','CODE','TABLE','IMAGE','HEADING')),
  CONSTRAINT fk_201 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_202 FOREIGN KEY (tenant_id,document_id,build_id) REFERENCES index_build (tenant_id,document_id,id),
  CONSTRAINT fk_203 FOREIGN KEY (tenant_id,document_id,artifact_id) REFERENCES artifact (tenant_id,document_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='结构化文档源单元表，存储版面解析出的基本元素块及其在源文档中的排版位置';

CREATE TABLE chunk (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '切片唯一标识符 (UUID)',
  build_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属索引构建标识符',
  parent_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '父级切片标识符 (父子分块 Parent-Child 结构，顶级父切片为 NULL)',
  kind VARCHAR(8) NOT NULL COMMENT '切片层级类型 (PARENT: 父切片/大窗口上下文, CHILD: 子切片/检索命中颗粒)',
  ordinal INT NOT NULL COMMENT '构建版本内同类型切片的顺序编号',
  text_content LONGTEXT NOT NULL COMMENT '切片文本正文内容',
  token_count INT NOT NULL COMMENT '切片所占用的 Token 标记数量',
  content_hash BINARY(32) NOT NULL COMMENT '切片正文内容的 SHA-256 哈希值 (32字节)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '切片创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '切片更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_chunk_build (tenant_id,build_id,id),
  UNIQUE KEY uq_chunk_ordinal (tenant_id,build_id,kind,ordinal),
  CHECK (kind IN ('PARENT','CHILD')),
  CHECK (token_count >= 0),
  CHECK (parent_id IS NULL OR parent_id <> id),
  CONSTRAINT fk_32 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_33 FOREIGN KEY (tenant_id,build_id) REFERENCES index_build (tenant_id,id),
  CONSTRAINT fk_34 FOREIGN KEY (tenant_id,build_id,parent_id) REFERENCES chunk (tenant_id,build_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='检索切片分块表，存储面向检索划分的文本块及其父子层级结构';

CREATE TABLE chunk_span (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '映射跨度唯一标识符 (UUID)',
  build_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属索引构建标识符',
  chunk_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属检索切片标识符',
  unit_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '引用的源单元标识符',
  ordinal INT NOT NULL COMMENT '切片内部跨度顺序序号',
  start_cp INT NOT NULL COMMENT '源单元文本内的起始字符偏移量 (Unicode Code Point)',
  end_cp INT NOT NULL COMMENT '源单元文本内的结束字符偏移量 (Unicode Code Point)',
  location JSON NULL COMMENT '精细版面区域坐标位置的 JSON 补充描述',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '跨度记录创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '跨度记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_span_order (tenant_id,chunk_id,ordinal),
  CHECK (start_cp >= 0 AND end_cp > start_cp),
  CONSTRAINT fk_35 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_36 FOREIGN KEY (tenant_id,build_id,chunk_id) REFERENCES chunk (tenant_id,build_id,id),
  CONSTRAINT fk_37 FOREIGN KEY (tenant_id,build_id,unit_id) REFERENCES source_unit (tenant_id,build_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='切片与源单元映射跨度表，精确追踪每个检索切片在源文档结构单元中的字符偏移与版面位置';

CREATE TABLE representation (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '表示形式唯一标识符 (UUID)',
  build_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属索引构建标识符',
  chunk_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '目标切片标识符',
  kind VARCHAR(16) NOT NULL COMMENT '文本表示形式类别 (ORIGINAL: 原始切片, SUMMARY: 摘要表示, QUESTION: 假设问题/HyDE)',
  ordinal INT NOT NULL DEFAULT 0 COMMENT '同类型表示的多重序号 (非 QUESTION 时固定为 0)',
  text_content LONGTEXT NOT NULL COMMENT '对应表示形式的文本正文',
  text_hash BINARY(32) NOT NULL COMMENT '表示文本正文的 SHA-256 哈希指纹 (32字节)',
  model_fingerprint VARCHAR(255) NULL COMMENT '生成向量的 Embedding 模型指纹',
  dims INT NULL COMMENT '稠密向量维度 (如 1536, 与 vector_bytes 严格对应)',
  vector_bytes LONGBLOB NULL COMMENT '稠密向量 IEEE 754 浮点数序列的原始二进制字节流 (字节长度为 dims * 4)',
  vector_hash BINARY(32) NULL COMMENT '向量二进制内容的 SHA-256 校验哈希值 (32字节)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '向量表示记录创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '向量表示记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_rep_kind (tenant_id,chunk_id,kind,ordinal),
  KEY ix_rep_build (tenant_id,build_id),
  CHECK (kind IN ('ORIGINAL','SUMMARY','QUESTION')),
  CHECK (ordinal >= 0 AND (kind='QUESTION' OR ordinal=0)),
  CHECK ((vector_bytes IS NULL AND dims IS NULL) OR (vector_bytes IS NOT NULL AND dims IS NOT NULL AND dims > 0 AND OCTET_LENGTH(vector_bytes)=4*dims)),
  CONSTRAINT fk_204 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_205 FOREIGN KEY (tenant_id,build_id,chunk_id) REFERENCES chunk (tenant_id,build_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='切片多表示与稠密向量表，存储切片的多模态/假设问题/摘要表示及其 Embedding 向量二进制';

CREATE TABLE job (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '异步作业唯一标识符 (UUID)',
  kind VARCHAR(24) NOT NULL COMMENT '任务业务类型 (INGEST: 文档摄取, PURGE: 物理清除, RESTORE: 还原恢复, EVALUATE: 离线评测, EXPORT: 导出备份, CLEANUP: 垃圾清理)',
  target_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '任务关联的目标实体标识符 (多态关联 document/dataset 等)',
  requested_by CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '提交触发任务的用户标识符',
  idempotency_key VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '业务幂等唯一键，防止重复提交',
  request_hash BINARY(32) NOT NULL COMMENT '提交请求参数体的 SHA-256 哈希指纹 (32字节)',
  status VARCHAR(24) NOT NULL DEFAULT 'QUEUED' COMMENT '任务执行状态 (QUEUED: 排队中, RUNNING: 执行中, SUCCEEDED: 成功, FAILED: 失败, CANCELLED: 已取消)',
  stage VARCHAR(32) NOT NULL COMMENT '任务当前执行阶段 (例如 EXTRACTING, CHUNKING, INDEXING)',
  attempt INT NOT NULL DEFAULT 0 COMMENT '已尝试执行/重试的次数',
  available_at DATETIME(6) NOT NULL COMMENT '任务允许被 Worker 抢占领取的就绪时间戳 (UTC)',
  lease_until DATETIME(6) NULL COMMENT '分布式租约到期时间戳 (UTC, 用于 Worker 心跳续期与超时自动释放)',
  lease_epoch BIGINT NOT NULL DEFAULT 0 COMMENT '租约纪元版本号，用于 CAS 乐观锁防多节点重复领取',
  fence BIGINT NULL COMMENT '分布式防脑裂隔离栅栏值',
  checkpoint JSON NULL COMMENT '任务执行断点与上下文状态的 JSON 载体 (支持断点续传)',
  error_code VARCHAR(64) NULL COMMENT '任务失败时的错误码标识',
  cancel_requested_at DATETIME(6) NULL COMMENT '用户发起取消请求的时间戳 (UTC, NULL表示未请求取消)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '任务创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '任务更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_job_idempotency (tenant_id,kind,idempotency_key),
  KEY ix_job_claim (status,available_at,lease_until),
  CHECK (kind IN ('INGEST','PURGE','RESTORE','EVALUATE','EXPORT','CLEANUP')),
  CONSTRAINT fk_40 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_41 FOREIGN KEY (tenant_id,requested_by) REFERENCES app_user (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='异步批处理调度作业表，驱动文档解析摄取、清理、离线评测等长生命周期任务的状态机与分布式租约调度';

CREATE TABLE conversation (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '对话会话唯一标识符 (UUID)',
  owner_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '会话创建者/所有者用户标识符',
  title VARCHAR(255) NULL COMMENT '对话标题',
  status VARCHAR(16) NOT NULL DEFAULT 'ACTIVE' COMMENT '会话生命周期状态 (ACTIVE: 活跃进行中, ARCHIVED: 已归档)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '对话会话创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '对话会话更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_conversation_owner (tenant_id,owner_id,id),
  CONSTRAINT fk_42 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_43 FOREIGN KEY (tenant_id,owner_id) REFERENCES app_user (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='多轮问答对话会话表，维护用户维度的对话历史上下文载体';

CREATE TABLE context_segment (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '上下文分段唯一标识符 (UUID)',
  conversation_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属对话会话标识符',
  segment_no INT NOT NULL COMMENT '对话内分段单调递增序号 (从 1 开始)',
  scope_mode VARCHAR(8) NOT NULL COMMENT '知识库检索范围模式 (ALL: 检索用户有权访问的所有知识库, SELECTED: 仅检索当前分段指定的知识库)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '上下文分段创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '上下文分段更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_segment_no (tenant_id,conversation_id,segment_no),
  UNIQUE KEY uq_segment_conv (tenant_id,conversation_id,id),
  CHECK (scope_mode IN ('ALL','SELECTED')),
  CONSTRAINT fk_44 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_45 FOREIGN KEY (tenant_id,conversation_id) REFERENCES conversation (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='会话上下文分段表，管理对话过程中检索范围切换与上下文窗口重置切分';

CREATE TABLE segment_kb (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '绑定关联唯一标识符 (UUID)',
  segment_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属上下文分段标识符',
  kb_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '绑定的目标知识库标识符',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '绑定关系创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '绑定关系更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_segment_kb (tenant_id,segment_id,kb_id),
  CONSTRAINT fk_46 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_47 FOREIGN KEY (tenant_id,segment_id) REFERENCES context_segment (tenant_id,id),
  CONSTRAINT fk_48 FOREIGN KEY (tenant_id,kb_id) REFERENCES knowledge_base (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='上下文分段知识库绑定表，定义 SELECTED 模式下分段生效的目标知识库列表';

CREATE TABLE qa_run (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '问答运行流水唯一标识符 (UUID)',
  owner_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '提问用户标识符',
  conversation_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属对话会话标识符',
  segment_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属上下文分段标识符',
  turn_no INT NOT NULL COMMENT '会话内的对话轮次序号 (从 1 开始)',
  config_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '本轮问答生效的 RETRIEVAL/MODEL 配置版本标识符',
  mode VARCHAR(8) NOT NULL COMMENT '问答管线执行模式 (RAG: 标准检索增强, AGENT: 多步智能体推理)',
  strict_mode BOOLEAN NOT NULL DEFAULT FALSE COMMENT '严格模式标识 (为 true 时若证据不足则直接拒答，杜绝模型幻觉)',
  status VARCHAR(32) NOT NULL DEFAULT 'QUEUED' COMMENT '运行状态 (QUEUED: 排队中, RUNNING: 生成中, SUCCEEDED: 成功, FAILED: 失败, CANCELLED: 已取消)',
  stage VARCHAR(32) NOT NULL COMMENT '当前执行阶段 (如 ROUTING, RETRIEVING, GENERATING, VALIDATING)',
  idempotency_key VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '客户端请求幂等唯一键',
  request_hash BINARY(32) NOT NULL COMMENT '提问请求参数 SHA-256 哈希值 (32字节)',
  question LONGTEXT NULL COMMENT '用户提问原始文本内容',
  result_json JSON NULL COMMENT '模型最终生成回复及断言检验结果的 JSON 结构体',
  security_epoch BIGINT NOT NULL COMMENT '本轮问答执行时绑定的租户安全纪元号 (用于鉴权快照一致性校验)',
  budget_json JSON NOT NULL COMMENT '资金与 Token 预扣配额信息的 JSON 结构体',
  terminal_reason VARCHAR(64) NULL COMMENT '终态原因说明 (如 COMPLETED, REFUSAL_NO_EVIDENCE, TIMEOUT 等)',
  cancel_requested_at DATETIME(6) NULL COMMENT '用户主动取消生成的时间戳 (UTC, NULL表示未取消)',
  started_at DATETIME(6) NULL COMMENT '问答开始执行时间戳 (UTC)',
  finished_at DATETIME(6) NULL COMMENT '问答执行结束时间戳 (UTC)',
  active_conversation_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin GENERATED ALWAYS AS (CASE WHEN status IN ('QUEUED','RUNNING') THEN conversation_id ELSE NULL END) STORED COMMENT '活跃状态下的对话标识符 (虚拟存储生成列，仅在 QUEUED/RUNNING 时非空，用于唯一索引保障同会话串行并发互斥)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '运行记录创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '运行记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_active_conversation (tenant_id,active_conversation_id),
  UNIQUE KEY uq_run_idempotency (tenant_id,owner_id,idempotency_key),
  UNIQUE KEY uq_run_turn (tenant_id,conversation_id,turn_no),
  CHECK (mode IN ('RAG','AGENT')),
  CONSTRAINT fk_49 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_50 FOREIGN KEY (tenant_id,owner_id,conversation_id) REFERENCES conversation (tenant_id,owner_id,id),
  CONSTRAINT fk_51 FOREIGN KEY (tenant_id,conversation_id,segment_id) REFERENCES context_segment (tenant_id,conversation_id,id),
  CONSTRAINT fk_52 FOREIGN KEY (tenant_id,config_id) REFERENCES config_revision (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='在线知识问答运行记录表，记录每一轮 RAG/Agent 问答的生命周期、状态机流转、生成结果及审计信息';

CREATE TABLE run_source (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '证据溯源记录唯一标识符 (UUID)',
  run_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属问答运行流水标识符',
  document_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '召回命中的目标文档标识符',
  build_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '命中文档所采用的索引构建版本标识符',
  unit_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '对应具体命中的源单元标识符 (可选)',
  evidence_id VARCHAR(32) NULL COMMENT '客户端前台引用角标编号 (如 [1], [2])',
  purpose VARCHAR(16) NOT NULL COMMENT '证据用途类型 (RETRIEVED: 初筛召回, RERANKED: 重排入选, CITATION: 最终回答引用)',
  span_json JSON NULL COMMENT '命中切片或版面高亮范围详情 JSON',
  payload_hash BINARY(32) NULL COMMENT '引用文本内容快照的 SHA-256 哈希值 (32字节)',
  send_status VARCHAR(16) NOT NULL DEFAULT 'PREPARED' COMMENT '证据发送推送状态 (PREPARED: 已准备, SENT: 已推送至前端, SUPPRESSED: 护栏阻断抑制)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '证据记录创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '证据记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  KEY ix_source_document (tenant_id,document_id,run_id),
  KEY ix_source_run (tenant_id,run_id),
  CONSTRAINT fk_53 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_54 FOREIGN KEY (tenant_id,run_id) REFERENCES qa_run (tenant_id,id),
  CONSTRAINT fk_55 FOREIGN KEY (tenant_id,document_id,build_id) REFERENCES index_build (tenant_id,document_id,id),
  CONSTRAINT fk_56 FOREIGN KEY (tenant_id,build_id,unit_id) REFERENCES source_unit (tenant_id,build_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='问答证据引用溯源表，详细记录问答推理所检索、引用及推送给用户的文档片段与佐证来源';

CREATE TABLE run_dependency (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '依赖记录唯一标识符 (UUID)',
  run_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '当前问答运行流水标识符',
  prior_run_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所依赖的前序问答运行流水标识符',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '依赖记录创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '依赖记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_run_dependency (tenant_id,run_id,prior_run_id),
  CHECK (run_id <> prior_run_id),
  CONSTRAINT fk_57 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_58 FOREIGN KEY (tenant_id,run_id) REFERENCES qa_run (tenant_id,id),
  CONSTRAINT fk_59 FOREIGN KEY (tenant_id,prior_run_id) REFERENCES qa_run (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='多轮问答轮次依赖关系表，记录多轮对话上下文历史的 DAG 依赖拓扑';

CREATE TABLE run_event (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '事件唯一标识符 (UUID)',
  run_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属问答运行流水标识符',
  sequence_no INT NOT NULL COMMENT '问答内部事件单调递增序号 (从 1 开始)',
  stage VARCHAR(32) NOT NULL COMMENT '触发事件所处的执行阶段 (如 RETRIEVE_START, RETRIEVE_END, LLM_FIRST_TOKEN)',
  duration_ms BIGINT NULL COMMENT '该执行阶段消耗的毫秒时长',
  payload JSON NULL COMMENT '事件关联的上下文明细与指标数据的 JSON 结构体',
  error_code VARCHAR(64) NULL COMMENT '异常失败时的错误码 (若发生异常)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '事件发生时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '事件记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_event_sequence (tenant_id,run_id,sequence_no),
  CONSTRAINT fk_60 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_61 FOREIGN KEY (tenant_id,run_id) REFERENCES qa_run (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='问答执行阶段流水事件表，追踪单次问答生命周期中的细粒度时间线、阶段耗时与可观测性事件';

CREATE TABLE feedback (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '反馈记录唯一标识符 (UUID)',
  run_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '评价的目标问答运行流水标识符',
  submitted_by CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '提交反馈的用户标识符',
  rating VARCHAR(8) NULL COMMENT '评价等级 (USEFUL: 有用/点赞, USELESS: 无用/点踩)',
  issue_type VARCHAR(32) NULL COMMENT '反馈问题分类 (如 HALLUCINATION: 幻觉, INCOMPLETE: 回答不完整, IRRELEVANT: 不相关等)',
  comment_text TEXT NULL COMMENT '用户填写的详细文本反馈内容',
  status VARCHAR(16) NOT NULL DEFAULT 'OPEN' COMMENT '反馈工单处理状态 (OPEN: 待处理, IN_PROGRESS: 调查中, RESOLVED: 已解决)',
  resolution_text TEXT NULL COMMENT '运营或技术人员填写的排查与改进结论',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '反馈创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '反馈更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_feedback_run_user (tenant_id,run_id,submitted_by),
  CHECK (rating IS NULL OR rating IN ('USEFUL','USELESS')),
  CHECK (status IN ('OPEN','IN_PROGRESS','RESOLVED')),
  CONSTRAINT fk_62 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_63 FOREIGN KEY (tenant_id,run_id) REFERENCES qa_run (tenant_id,id),
  CONSTRAINT fk_64 FOREIGN KEY (tenant_id,submitted_by) REFERENCES app_user (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='用户问答质量反馈表，收集终端用户对模型生成回答的点赞、点踩、问题归类及运营处理结果';

CREATE TABLE tenant_budget (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '预算配置唯一标识符 (UUID)',
  period_month CHAR(7) CHARACTER SET ascii NOT NULL COMMENT '预算生效月份周期 (格式: YYYY-MM)',
  currency CHAR(3) NOT NULL DEFAULT 'CNY' COMMENT '结算币种代码 (如 CNY, USD)',
  limit_amount DECIMAL(18,6) NOT NULL COMMENT '本月度预算额度上限金额',
  reserved_amount DECIMAL(18,6) NOT NULL DEFAULT 0 COMMENT '当前已被并发调用临时预扣/锁定的资金总额',
  settled_amount DECIMAL(18,6) NOT NULL DEFAULT 0 COMMENT '本月度已实际完成计费结算的累计消耗金额',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '预算记录创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '预算记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_budget_period (tenant_id,period_month),
  CHECK (limit_amount >= 0 AND reserved_amount >= 0 AND settled_amount >= 0),
  CONSTRAINT fk_65 FOREIGN KEY (tenant_id) REFERENCES tenant (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='租户月度资金预算表，控制和审计租户各自然月的大模型调用消费限额与预扣金额';

CREATE TABLE budget_reservation (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '预扣流水唯一标识符 (UUID)',
  budget_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '关联的租户月度预算标识符',
  caller_environment VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '调用方运行环境标识 (如 PRODUCTION, STAGING, EVALUATION)',
  caller_call_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '调用方发起的调用请求全局唯一标识符',
  status VARCHAR(16) NOT NULL COMMENT '预扣流水状态 (RESERVED: 已预扣, SETTLED: 已结算扣减, RELEASED: 已取消释放, EXPIRED: 超时自动释放)',
  reserved_amount DECIMAL(18,6) NOT NULL COMMENT '本次调用申请锁定的最大预扣金额',
  settled_amount DECIMAL(18,6) NULL COMMENT '调用完成后实际核销结算的最终金额 (NULL表示尚未结算)',
  expires_at DATETIME(6) NOT NULL COMMENT '预扣配额锁定过期时间戳 (UTC, 超时未结算将由后台对账任务自动释放)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '预扣记录创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '预扣记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id,id),
  UNIQUE KEY uq_reservation_call (tenant_id,caller_environment,caller_call_id),
  KEY ix_reservation_status (tenant_id,status,expires_at),
  CHECK (reserved_amount >= 0),
  CONSTRAINT fk_206 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_207 FOREIGN KEY (tenant_id,budget_id) REFERENCES tenant_budget (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='模型调用资金预扣流水表，实现大模型 API 调用的两阶段资金预扣与实际消耗结算对账';

CREATE TABLE model_call (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '模型调用记录唯一标识符 (UUID)',
  run_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '关联的在线问答运行流水标识符 (与 job_id 互斥，二者必居其一)',
  job_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '关联的异步后台作业标识符 (与 run_id 互斥，二者必居其一)',
  purpose VARCHAR(24) NOT NULL COMMENT '模型调用业务目的 (如 CHAT_COMPLETION, EMBEDDING, RERANK, QUERY_REWRITE)',
  model_config_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '本次调用生效的 MODEL 类别配置版本标识符',
  attempt_no INT NOT NULL COMMENT '针对该请求的调用重试序号 (从 1 开始)',
  status VARCHAR(16) NOT NULL COMMENT '调用结果状态 (SUCCESS: 成功, FAILED: 失败, TIMEOUT: 超时, RATE_LIMITED: 限流)',
  reserved_amount DECIMAL(18,6) NOT NULL COMMENT '预扣锁定的预算金额',
  settled_amount DECIMAL(18,6) NULL COMMENT '最终核算的实际消耗金额',
  input_tokens BIGINT NULL COMMENT '提示词输入的 Token 标记数量',
  output_tokens BIGINT NULL COMMENT '模型回复输出的 Token 标记数量',
  provider_request_id VARCHAR(255) NULL COMMENT '模型服务商返回的全局请求追踪 ID (如 OpenAI request_id)',
  pricing_snapshot JSON NOT NULL COMMENT '调用发生时刻模型单价与计费规则的 JSON 静态快照',
  usage_detail JSON NULL COMMENT '模型服务商返回的详细使用量与耗时分解 JSON',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '调用记录创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '调用记录更新时间戳 (UTC)',
  reservation_authority VARCHAR(128) NOT NULL COMMENT '预扣执行的权威校验主体',
  reservation_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '关联的资金预扣流水记录标识符',
  PRIMARY KEY (tenant_id, id),
  KEY ix_call_run (tenant_id,run_id),
  KEY ix_call_job (tenant_id,job_id),
  CHECK ((run_id IS NULL) <> (job_id IS NULL)),
  CHECK (reserved_amount >= 0),
  CONSTRAINT fk_208 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_209 FOREIGN KEY (tenant_id,run_id) REFERENCES qa_run (tenant_id,id),
  CONSTRAINT fk_210 FOREIGN KEY (tenant_id,job_id) REFERENCES job (tenant_id,id),
  CONSTRAINT fk_211 FOREIGN KEY (tenant_id,model_config_id) REFERENCES config_revision (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='大模型API调用审计明细表，完整记录所有模型调用的入参Token、出参Token、费用核算与服务商响应明细';

CREATE TABLE eval_dataset_version (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '评测数据集版本唯一标识符 (UUID)',
  name VARCHAR(128) NOT NULL COMMENT '数据集标识名称',
  revision INT NOT NULL COMMENT '数据集单调递增版本号 (从 1 开始)',
  status VARCHAR(16) NOT NULL DEFAULT 'DRAFT' COMMENT '数据集生命周期状态 (DRAFT: 编辑草稿中, FROZEN: 已冻结不可变, PURGED: 已清除)',
  manifest_hash BINARY(32) NULL COMMENT '数据集完整测试用例内容的清单 SHA-256 校验哈希值 (32字节)',
  frozen_at DATETIME(6) NULL COMMENT '数据集版本正式封存冻结的时间戳 (UTC, 冻结后禁止修改以保证评测基准客观可复现)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '数据集版本创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '数据集版本更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_dataset_revision (tenant_id,name,revision),
  CHECK (status IN ('DRAFT','FROZEN','PURGED')),
  CONSTRAINT fk_71 FOREIGN KEY (tenant_id) REFERENCES tenant (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='评测题集版本表，管理离线基准评测数据集的版本迭代与基线冻结';

CREATE TABLE eval_case (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '评测测试用例唯一标识符 (UUID)',
  dataset_version_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属评测数据集版本标识符',
  case_key VARCHAR(128) NOT NULL COMMENT '版本内用例唯一业务键',
  split VARCHAR(8) NOT NULL COMMENT '数据集划分类型 (DEV: 开发验证集, TEST: 盲测基准集)',
  group_key VARCHAR(128) NOT NULL COMMENT '业务分组/场景键 (用于按场景聚合评测指标)',
  category VARCHAR(32) NOT NULL COMMENT '问题分类标签 (如 SINGLE_HOP, MULTI_HOP, TEMPORAL, REFUSAL)',
  question_json JSON NOT NULL COMMENT '评测提问内容及多轮前置上下文的 JSON 结构体',
  persona_json JSON NOT NULL COMMENT '模拟提问者用户角色与权限身份的 JSON 配置',
  expected_json JSON NOT NULL COMMENT '期望答案与提取规则的 JSON 结构体',
  annotation_status VARCHAR(16) NOT NULL COMMENT '用例人工标注状态 (如 PENDING: 待标注, VERIFIED: 已核验)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '测试用例创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '测试用例更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_case_key (tenant_id,dataset_version_id,case_key),
  UNIQUE KEY uq_case_dataset (tenant_id,dataset_version_id,id),
  CHECK (split IN ('DEV','TEST')),
  CONSTRAINT fk_72 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_73 FOREIGN KEY (tenant_id,dataset_version_id) REFERENCES eval_dataset_version (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='评测测试用例表，存储包含提问输入、角色画像、期望输出与分类标签的单个测试题';

CREATE TABLE eval_gold (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '金标准记录唯一标识符 (UUID)',
  case_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '关联的评测测试用例标识符',
  fact_group VARCHAR(128) NOT NULL COMMENT '事实断言分组标识符 (用于多事实点召回组合覆盖判定)',
  alternative_no INT NOT NULL COMMENT '相同事实的不同等价表述版本序号',
  origin_system VARCHAR(128) NOT NULL COMMENT '溯源系统标识',
  origin_tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '溯源租户标识符',
  origin_document_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '原始事实所来源的文档标识符',
  version_hash BINARY(32) NOT NULL COMMENT '引用的文档版本内容 SHA-256 哈希校验值 (32字节)',
  anchor_json JSON NOT NULL COMMENT '金标准事实片段在源文档中的精确定位锚点 JSON',
  relevance_grade INT NOT NULL COMMENT '事实与问题的相关性分级得分 (0 到 3 级)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '金标准记录创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '金标准记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  CHECK (relevance_grade BETWEEN 0 AND 3),
  KEY ix_gold_origin (origin_system,origin_tenant_id,origin_document_id),
  CONSTRAINT fk_74 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_75 FOREIGN KEY (tenant_id,case_id) REFERENCES eval_case (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='评测金标准事实锚点表，维护用于验证检索命中率与忠实度的标准答案佐证片段与相关度分级';

CREATE TABLE eval_snapshot (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '评测快照唯一标识符 (UUID)',
  name VARCHAR(128) NOT NULL COMMENT '快照显示名称',
  manifest_hash BINARY(32) NOT NULL COMMENT '快照包含全部文档及版本清单的 SHA-256 哈希防伪指纹 (32字节)',
  manifest_json JSON NOT NULL COMMENT '快照文档与版本清单详情的 JSON 结构体',
  status VARCHAR(16) NOT NULL COMMENT '快照生命周期状态 (如 ACTIVE: 可用, ARCHIVED: 已归档)',
  environment_id VARCHAR(128) NOT NULL COMMENT '评测绑定的物理/虚拟执行隔离环境标识符',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '评测快照创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '评测快照更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT fk_76 FOREIGN KEY (tenant_id) REFERENCES tenant (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='评测资料环境快照表，固化评测执行时依赖的知识库文档与构建版本上下文环境';

CREATE TABLE eval_snapshot_document (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '快照文档条目唯一标识符 (UUID)',
  snapshot_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属评测快照标识符',
  document_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '锁定的目标文档标识符',
  version_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '锁定的目标文档版本标识符',
  origin_system VARCHAR(128) NOT NULL COMMENT '源系统标识',
  origin_tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '源租户标识符',
  origin_document_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '源文档标识符',
  origin_version_hash BINARY(32) NOT NULL COMMENT '源文档版本内容的 SHA-256 哈希值 (32字节)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '快照条目创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '快照条目更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_snapshot_doc (tenant_id,snapshot_id,document_id),
  KEY ix_snapshot_origin (origin_system,origin_tenant_id,origin_document_id),
  CONSTRAINT fk_77 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_78 FOREIGN KEY (tenant_id,snapshot_id) REFERENCES eval_snapshot (tenant_id,id),
  CONSTRAINT fk_79 FOREIGN KEY (tenant_id,document_id,version_id) REFERENCES document_version (tenant_id,document_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='评测快照文档条目表，记录快照集合中包含的每一篇具体文档及其固化的版本信息';

CREATE TABLE eval_run (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '评测运行批次唯一标识符 (UUID)',
  dataset_version_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所执行的评测数据集版本标识符',
  snapshot_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所依赖的资料环境快照标识符',
  config_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所评测的 RETRIEVAL/MODEL 系统策略配置版本标识符',
  job_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '驱动该评测批次执行的后台异步作业标识符',
  status VARCHAR(24) NOT NULL COMMENT '运行状态 (RUNNING: 评测中, COMPLETED: 已完成, FAILED: 失败)',
  manifest_json JSON NOT NULL COMMENT '评测批次执行参数、用例范围等元信息的静态快照 JSON',
  metrics_json JSON NULL COMMENT '汇总聚合计算出的各项指标得分 JSON (如 Recall@K, MRR, Faithfulness, NDCG)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '评测运行记录创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '评测运行记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_eval_run_dataset (tenant_id,dataset_version_id,id),
  CONSTRAINT fk_80 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_81 FOREIGN KEY (tenant_id,dataset_version_id) REFERENCES eval_dataset_version (tenant_id,id),
  CONSTRAINT fk_82 FOREIGN KEY (tenant_id,snapshot_id) REFERENCES eval_snapshot (tenant_id,id),
  CONSTRAINT fk_83 FOREIGN KEY (tenant_id,config_id) REFERENCES config_revision (tenant_id,id),
  CONSTRAINT fk_84 FOREIGN KEY (tenant_id,job_id) REFERENCES job (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='离线评测批次运行表，管理一次完整的离线端到端或单组件评测实验执行及其聚合评估指标';

CREATE TABLE eval_result (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '单题评测结果唯一标识符 (UUID)',
  eval_run_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属评测运行批次标识符',
  dataset_version_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属评测数据集版本标识符',
  case_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '评测的测试用例标识符',
  repeat_no INT NOT NULL DEFAULT 0 COMMENT '相同用例在同一批次中的重复测试序号 (用于评测方差与稳定性，默认 0)',
  qa_run_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '评测执行过程中实际触发的底层问答流水标识符 (可选)',
  status VARCHAR(24) NOT NULL COMMENT '用例评测状态 (SUCCESS: 成功, FAILED: 失败, SKIPPED: 跳过)',
  metrics_json JSON NULL COMMENT '单题评测指标结果详情 JSON (包含检索召回度、回答精准度等单题得分)',
  judge_json JSON NULL COMMENT 'LLM-as-a-Judge 大模型裁判给出的打分理由、评语与断言解析 JSON',
  error_code VARCHAR(64) NULL COMMENT '评测失败时的错误码',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '结果记录创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '结果记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  UNIQUE KEY uq_eval_result (tenant_id,eval_run_id,case_id,repeat_no),
  CONSTRAINT fk_85 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_86 FOREIGN KEY (tenant_id,dataset_version_id,eval_run_id) REFERENCES eval_run (tenant_id,dataset_version_id,id),
  CONSTRAINT fk_87 FOREIGN KEY (tenant_id,dataset_version_id,case_id) REFERENCES eval_case (tenant_id,dataset_version_id,id),
  CONSTRAINT fk_88 FOREIGN KEY (tenant_id,qa_run_id) REFERENCES qa_run (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='单题单次评测结果明细表，记录离线评测批次中每个测试用例的运行输出、打分结果与大模型裁判判定详情';

CREATE TABLE eval_review (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '人工复核记录唯一标识符 (UUID)',
  result_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '被复核的目标单题评测结果标识符',
  reviewer_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '执行复核的人工专家用户标识符',
  labels_json JSON NOT NULL COMMENT '人工专家打标与修正评定数据的 JSON 结构体',
  reason_text TEXT NULL COMMENT '人工专家给出的复核依据与判定理由说明',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '复核记录创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '复核记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  CONSTRAINT fk_89 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_90 FOREIGN KEY (tenant_id,result_id) REFERENCES eval_result (tenant_id,id),
  CONSTRAINT fk_91 FOREIGN KEY (tenant_id,reviewer_id) REFERENCES app_user (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='评测结果人工复核表，记录专家用户对自动评测结果与大模型裁判打分的人工抽检与二次修正';

CREATE TABLE audit_event (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '审计事件唯一标识符 (UUID)',
  actor_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL COMMENT '执行操作的用户标识符 (NULL表示系统后台任务或匿名请求)',
  action VARCHAR(64) NOT NULL COMMENT '执行的操作行为动作 (如 CREATE_KB, DELETE_DOC, GRANT_PERMISSION, CALL_MODEL)',
  object_type VARCHAR(32) NOT NULL COMMENT '目标资源对象类型 (如 DOCUMENT, KNOWLEDGE_BASE, CONFIG)',
  object_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '目标资源对象的唯一标识符',
  outcome VARCHAR(16) NOT NULL COMMENT '操作结果 (SUCCESS: 成功, DENIED: 鉴权拒绝, FAILED: 失败)',
  metadata_json JSON NULL COMMENT '操作请求参数、IP、UA及上下文环境等审计补充数据的 JSON 结构体',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '审计事件发生时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '审计记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  KEY ix_audit_object (tenant_id,object_type,object_id,created_at),
  CONSTRAINT fk_92 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_93 FOREIGN KEY (tenant_id,actor_id) REFERENCES app_user (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='系统安全审计日志表，留存所有敏感操作、鉴权事件与资源变更流水以供合规审查';

CREATE TABLE deletion_record (
  tenant_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '所属租户唯一标识符',
  id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '删除记录唯一标识符 (UUID)',
  document_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '被标记删除的目标文档标识符',
  origin_system VARCHAR(128) NOT NULL COMMENT '来源系统标识',
  requested_by CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '发起删除请求的用户标识符',
  status VARCHAR(24) NOT NULL COMMENT '级联清理流转状态 (PENDING: 待处理, PROCESSING: 清理中, COMPLETED: 清理完成, FAILED: 失败)',
  domains_json JSON NOT NULL COMMENT '涉及级联清理的数据域范围列表 JSON (如 [MYSQL, ELASTICSEARCH, MILVUS, S3])',
  backup_expiry_at DATETIME(6) NOT NULL COMMENT '归档备份过期时间戳 (UTC, 超出后彻底物理销毁)',
  completed_at DATETIME(6) NULL COMMENT '全部级联清理彻底完成的时间戳 (UTC, NULL表示清理尚未完成)',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '记录创建时间戳 (UTC)',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '记录更新时间戳 (UTC)',
  PRIMARY KEY (tenant_id, id),
  KEY ix_deletion_doc (tenant_id,document_id),
  CONSTRAINT fk_94 FOREIGN KEY (tenant_id) REFERENCES tenant (id),
  CONSTRAINT fk_95 FOREIGN KEY (tenant_id,document_id) REFERENCES document (tenant_id,id),
  CONSTRAINT fk_96 FOREIGN KEY (tenant_id,requested_by) REFERENCES app_user (tenant_id,id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci COMMENT='文档级联删除传播记录表，追踪记录文档在关系数据库、向量库、全文索引及对象存储中的物理清理与生命周期终结';

-- Deferred circular reference: an active build must belong to this document.
ALTER TABLE document ADD CONSTRAINT fk_document_active_build
  FOREIGN KEY (tenant_id,id,active_build_id)
  REFERENCES index_build (tenant_id,document_id,id);

ALTER TABLE tenant ADD CONSTRAINT fk_tenant_retrieval_config
  FOREIGN KEY (id,active_retrieval_config_id)
  REFERENCES config_revision (tenant_id,id);
