package dev.rag.common;
public class ApiException extends RuntimeException {
 public final int status; public final String code;
 public ApiException(int status,String code,String message){ super(message);this.status=status;this.code=code; }
 public static ApiException missing(){return new ApiException(404,"RESOURCE_NOT_FOUND","资源不存在或不可访问");}
}

