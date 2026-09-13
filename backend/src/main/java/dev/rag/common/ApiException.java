package dev.rag.common;

/**
 * 统一 API 业务运行时异常。
 * 携带 HTTP 状态码、系统错误码及可读提示信息，供全局异常处理器转换为结构化响应。
 */
public class ApiException extends RuntimeException {
    /** HTTP 响应状态码（如 400、403、404、409、429 等） */
    public final int status;
    /** 业务错误代码（如 INVALID_INPUT、RESOURCE_NOT_FOUND 等） */
    public final String code;

    /**
     * 构造业务 API 异常。
     *
     * @param status  HTTP 响应状态码
     * @param code    系统业务错误代码字符串
     * @param message 异常描述及用户友好提示
     */
    public ApiException(int status, String code, String message) {
        super(message);
        this.status = status;
        this.code = code;
    }

    /**
     * 快速构建 404 资源不存在或不可访问异常实例。
     *
     * @return 状态码为 404、错误码为 RESOURCE_NOT_FOUND 的 ApiException 实例
     */
    public static ApiException missing() {
        return new ApiException(404, "RESOURCE_NOT_FOUND", "资源不存在或不可访问");
    }
}

