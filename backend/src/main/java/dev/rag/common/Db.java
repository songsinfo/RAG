package dev.rag.common;

import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;

import java.util.*;

/**
 * 轻量级数据库访问辅助组件。
 * 基于 Spring 的 NamedParameterJdbcTemplate，提供简洁的命名参数查询、增改及防注入验证。
 */
@Component
public class Db {
    /** 命名参数 JDBC 模板实例 */
    public final NamedParameterJdbcTemplate jdbc;

    /**
     * 构造 Db 组件实例。
     *
     * @param jdbc Spring 容器注入的 NamedParameterJdbcTemplate
     */
    public Db(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /**
     * 执行命名参数查询并返回多行结果列表。
     *
     * @param sql    带有命名占位符（如 :tenantId）的 SQL 语句
     * @param params 命名参数键值对映射
     * @return 查询结果列表，每行表示为字段名与值的 Map
     */
    public List<Map<String, Object>> list(String sql, Map<String, ?> params) {
        return jdbc.queryForList(sql, params);
    }

    /**
     * 执行命名参数查询并返回唯一一条记录。
     * 若未查询到记录则直接抛出 404 业务异常 (ApiException.missing())。
     *
     * @param sql    带有命名占位符的 SQL 语句
     * @param params 命名参数键值对映射
     * @return 首条记录数据 Map
     * @throws ApiException 若查询结果集为空
     */
    public Map<String, Object> one(String sql, Map<String, ?> params) {
        var rows = list(sql, params);
        if (rows.isEmpty()) throw ApiException.missing();
        return rows.getFirst();
    }

    /**
     * 执行命名参数查询并返回可选单条记录。
     *
     * @param sql    带有命名占位符的 SQL 语句
     * @param params 命名参数键值对映射
     * @return 包含记录的 Optional，若未查到则返回 Optional.empty()
     */
    public Optional<Map<String, Object>> optional(String sql, Map<String, ?> params) {
        var rows = list(sql, params);
        return rows.isEmpty() ? Optional.empty() : Optional.of(rows.getFirst());
    }

    /**
     * 执行 UPDATE、DELETE 等更新操作并返回受影响的行数。
     *
     * @param sql    带有命名占位符的 SQL 语句
     * @param params 命名参数键值对映射
     * @return 数据库受影响的行数
     */
    public int exec(String sql, Map<String, ?> params) {
        return jdbc.update(sql, params);
    }

    /**
     * 动态生成并执行单表单行 INSERT 语句。
     * <p>
     * <b>安全性校验说明：</b>为防止 SQL 注入风险，方法对表名和所有字段名均做严格正则校验（仅允许小写字母、数字及下划线 [a-z0-9_]+），
     * 且所有数据值均通过参数化占位符（:col）传递。
     *
     * @param table  目标数据库表名（必须匹配 [a-z0-9_]+）
     * @param values 待插入的字段与数值键值映射（字段名必须匹配 [a-z0-9_]+）
     * @throws IllegalArgumentException 若表名或任意字段名包含非法字符
     */
    public void insert(String table, Map<String, ?> values) {
        if (!table.matches("[a-z0-9_]+") || values.keySet().stream().anyMatch(k -> !k.matches("[a-z0-9_]+")))
            throw new IllegalArgumentException("Unsafe SQL identifier");
        jdbc.update("INSERT INTO " + table + " (" + String.join(",", values.keySet()) + ") VALUES (" + String.join(",", values.keySet().stream().map(k -> ":" + k).toList()) + ")", values);
    }

    /**
     * 变长参数构造参数映射工具方法。
     * 按照 key1, value1, key2, value2 顺序传入，返回按插入顺序保留的 LinkedHashMap。
     *
     * @param pairs 偶数个键值对参数，偶数位下标为 String 键，奇数位为对应数值
     * @return 构造出的命名参数 Map
     */
    public static Map<String, Object> params(Object... pairs) {
        Map<String, Object> p = new LinkedHashMap<>();
        for (int i = 0; i < pairs.length; i += 2) p.put((String) pairs[i], pairs[i + 1]);
        return p;
    }
}

