package dev.rag.common;

/**
 * 操作鉴权主体（Actor），封装当前请求上下文中的租户身份、用户标识及权限等级。
 *
 * @param tenantId 当前所属租户唯一标识
 * @param userId   当前操作用户唯一标识
 * @param admin    是否具备当前租户的管理员角色
 */
public record Actor(String tenantId, String userId, boolean admin) {
}

