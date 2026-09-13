package dev.rag.common;

import org.springframework.web.bind.annotation.*;
import org.springframework.http.ResponseEntity;

import java.util.Map;

/**
 * 全局 REST 控制器异常处理切面。
 * 统一捕获业务 ApiException 和未预期的系统异常，标准化返回 JSON 错误响应体与重试标识。
 */
@RestControllerAdvice
public class Errors {
    /**
     * 捕获并处理已知业务异常 (ApiException)。
     * 映射具体的 HTTP 状态码，并针对 503（服务不可用）和 429（限流）设置 retryable 为 true。
     *
     * @param e 捕获到的 ApiException 业务异常
     * @return 封装了 requestId、错误码、错误信息及重试建议的响应实体
     */
    @ExceptionHandler(ApiException.class)
    ResponseEntity<?> api(ApiException e) {
        return ResponseEntity.status(e.status).body(Map.of("requestId", Json.id(), "code", e.code, "message", e.getMessage(), "retryable", e.status == 503 || e.status == 429));
    }

    /**
     * 捕获并处理未预期的未知系统异常 (Exception)。
     * 统一返回 500 内部服务器错误，隐藏底层敏感堆栈，提示通过 requestId 与运行记录定位排查。
     *
     * @param e 捕获到的未受检或系统级未知异常
     * @return 封装了 500 状态码与通用 INTERNAL_ERROR 的响应实体
     */
    @ExceptionHandler(Exception.class)
    ResponseEntity<?> unexpected(Exception e) {
        return ResponseEntity.internalServerError().body(Map.of("requestId", Json.id(), "code", "INTERNAL_ERROR", "message", "处理失败，请使用运行记录定位问题", "retryable", false));
    }
}

