package dev.rag.common;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.util.*;

/**
 * JSON 序列化与数据处理公共工具类。
 * 封装 Jackson ObjectMapper 操作并提供安全的 Map 字段提取与类型转换辅助。
 */
public final class Json {
    /** 共享的 Jackson 核心 ObjectMapper 实例 */
    public static final ObjectMapper MAPPER = new ObjectMapper();

    private Json() {
    }

    /**
     * 将 Java 对象序列化为 JSON 格式字符串。
     *
     * @param value 待序列化的对象（如 Map、List、POJO 等）
     * @return JSON 字符串
     * @throws IllegalArgumentException 若序列化过程发生 IO 或映射异常
     */
    public static String write(Object value) {
        try {
            return MAPPER.writeValueAsString(value);
        } catch (Exception e) {
            throw new IllegalArgumentException("Invalid JSON value", e);
        }
    }

    /**
     * 将 JSON 字符串反序列化为 Map&lt;String, Object&gt; 对象。
     *
     * @param text JSON 格式字符串
     * @return 转换后的 Map 对象
     * @throws ApiException 若解析失败或格式不合法（返回 400 INVALID_JSON）
     */
    public static Map<String, Object> object(String text) {
        try {
            return MAPPER.readValue(text, new TypeReference<Map<String, Object>>() {
            });
        } catch (Exception e) {
            throw new ApiException(400, "INVALID_JSON", "JSON 对象无效");
        }
    }

    /**
     * 将 JSON 字符串反序列化为 List&lt;Map&lt;String, Object&gt;&gt; 列表。
     *
     * @param text JSON 数组格式字符串
     * @return 转换后的 Map 列表
     * @throws ApiException 若解析失败或格式不合法（返回 400 INVALID_JSON）
     */
    public static List<Map<String, Object>> objects(String text) {
        try {
            return MAPPER.readValue(text, new TypeReference<List<Map<String, Object>>>() {
            });
        } catch (Exception e) {
            throw new ApiException(400, "INVALID_JSON", "JSON 列表无效");
        }
    }

    /**
     * 从 Map 中安全提取指定键的字符串值。
     * 若键不存在或值为 null，则返回空字符串 ""。
     *
     * @param v   数据源 Map
     * @param key 目标键名
     * @return 对应的字符串表示，不存在时为空串
     */
    public static String str(Map<String, ?> v, String key) {
        Object x = v.get(key);
        return x == null ? "" : x.toString();
    }

    /**
     * 从 Map 中提取整数值。
     * 若键不存在或为 null，则返回指定的默认值；若存在但无法转换为整数，则抛出 ApiException。
     *
     * @param v        数据源 Map
     * @param key      目标键名
     * @param fallback 键不存在时的兜底默认值
     * @return 提取并转换后的整数
     * @throws ApiException 当字符串无法被解析为合法整数时（返回 400 INVALID_INPUT）
     */
    public static int integer(Map<String, ?> v, String key, int fallback) {
        Object x = v.get(key);
        if (x == null) return fallback;
        try {
            return Integer.parseInt(x.toString());
        } catch (NumberFormatException e) {
            throw new ApiException(400, "INVALID_INPUT", key + " 必须是整数");
        }
    }

    /**
     * 从 Map 中提取布尔值。
     * 若键不存在或为 null，返回指定默认值；否则按 Boolean.parseBoolean 解析。
     *
     * @param v        数据源 Map
     * @param key      目标键名
     * @param fallback 键不存在时的兜底默认值
     * @return 提取的布尔值
     */
    public static boolean bool(Map<String, ?> v, String key, boolean fallback) {
        Object x = v.get(key);
        return x == null ? fallback : Boolean.parseBoolean(x.toString());
    }

    /**
     * 生成标准 UUID 字符串（无中划线处理直接使用 toString()）。
     * 用于主键、运行 ID 及请求链路追踪 ID。
     *
     * @return 36 位的 UUID 字符串
     */
    public static String id() {
        return UUID.randomUUID().toString();
    }
}

