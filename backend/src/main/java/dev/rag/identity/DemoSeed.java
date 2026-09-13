package dev.rag.identity;

import dev.rag.common.*;
import org.springframework.boot.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;

import java.util.*;

/**
 * 演示与测试环境初始种子数据填充组件。
 * <p>
 * <b>行为准则：</b>
 * 1. 显式开启（Explicit opt-in）：仅在配置了 RAG_SEED_PASSWORD 且处于 demo/test/evaluation 隔离环境时触发；
 * 2. 幂等保障：若对应环境租户已存在则跳过，绝不在重启时重置已有生产数据。
 */
@Component
@org.springframework.context.annotation.DependsOn("flyway")
public class DemoSeed implements ApplicationRunner {
    private final Db db;
    private final TransactionTemplate tx;
    private final String password, environment;

    /**
     * 构造 DemoSeed 实例。
     *
     * @param db          数据库操作组件
     * @param tx          编程式事务模板
     * @param password    种子用户初始化密码（需不少于 12 位）
     * @param environment 当前运行环境标识（默认 demo）
     */
    public DemoSeed(Db db, TransactionTemplate tx, @Value("${RAG_SEED_PASSWORD:}") String password, @Value("${rag.environment:demo}") String environment) {
        this.db = db;
        this.tx = tx;
        this.password = password;
        this.environment = environment;
    }

    /**
     * 在 Spring 容器启动完成后执行种子数据预置逻辑。
     * <p>
     * <b>初始化流程：</b>
     * 1. 校验密码非空、环境隔离合法性及密码长度（>= 12 位）；
     * 2. 创建默认租户（若尚未初始化）；
     * 3. 预置基础检索配置版本（BM25 baseline）与默认模型计费配置版本；
     * 4. 创建默认用户矩阵（admin 管理员、ops 运维、product 产品、engineer 研发）及对应的用户组与成员绑定。
     *
     * @param args 命令行启动参数
     * @throws IllegalStateException 若在非隔离环境启用或密码强度不达标
     */
    public void run(ApplicationArguments args) {
        if (password.isBlank()) return;
        if (!Set.of("demo", "test", "evaluation").contains(environment))
            throw new IllegalStateException("Seed requires isolated environment");
        if (password.length() < 12) throw new IllegalStateException("Seed password requires at least 12 characters");
        String hash = new BCryptPasswordEncoder(12).encode(password);
        tx.executeWithoutResult(s -> {
            for (String code : List.of(environment)) {
                if (db.optional("SELECT id FROM tenant WHERE code=:c", Map.of("c", code)).isPresent()) continue;
                String t = Json.id();
                db.insert("tenant", Db.params("id", t, "code", code, "name", code + " 示例组织"));
                String configId = Json.id(), payload = "{\"retrieval\":{\"mode\":\"BM25\",\"representations\":[\"ORIGINAL\"]}}";
                db.insert("config_revision", Db.params("tenant_id", t, "id", configId, "kind", "RETRIEVAL", "name", "baseline", "revision", 1, "sha256", AccessPolicy.hash(payload), "payload", payload, "status", "PUBLISHED"));
                db.exec("UPDATE tenant SET active_retrieval_config_id=:c WHERE id=:t", Db.params("c", configId, "t", t));
                String modelId = Json.id(), modelPayload = "{\"currency\":\"CNY\",\"inputPricePerMillion\":1,\"outputPricePerMillion\":2}";
                db.insert("config_revision", Db.params("tenant_id", t, "id", modelId, "kind", "MODEL", "name", "default-pricing", "revision", 1, "sha256", AccessPolicy.hash(modelPayload), "payload", modelPayload, "status", "PUBLISHED"));
                for (String name : List.of("admin", "ops", "product", "engineer")) {
                    String u = Json.id();
                    db.insert("app_user", Db.params("tenant_id", t, "id", u, "login_name", name, "password_hash", hash, "is_admin", name.equals("admin")));
                    if (!name.equals("admin")) {
                        String g = Json.id();
                        db.insert("user_group", Db.params("tenant_id", t, "id", g, "name", name));
                        db.insert("group_member", Db.params("tenant_id", t, "id", Json.id(), "group_id", g, "user_id", u));
                    }
                }
            }
        });
    }
}
