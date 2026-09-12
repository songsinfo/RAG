package dev.rag.common;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.*;
public final class Json {
 public static final ObjectMapper MAPPER=new ObjectMapper();
 private Json(){}
 public static String write(Object value){try{return MAPPER.writeValueAsString(value);}catch(Exception e){throw new IllegalArgumentException("Invalid JSON value",e);}}
 public static Map<String,Object> object(String text){try{return MAPPER.readValue(text,new TypeReference<Map<String,Object>>(){});}catch(Exception e){throw new ApiException(400,"INVALID_JSON","JSON 对象无效");}}
 public static List<Map<String,Object>> objects(String text){try{return MAPPER.readValue(text,new TypeReference<List<Map<String,Object>>>(){});}catch(Exception e){throw new ApiException(400,"INVALID_JSON","JSON 列表无效");}}
 public static String str(Map<String,?> v,String key){Object x=v.get(key);return x==null?"":x.toString();}
 public static int integer(Map<String,?> v,String key,int fallback){Object x=v.get(key);if(x==null)return fallback;try{return Integer.parseInt(x.toString());}catch(NumberFormatException e){throw new ApiException(400,"INVALID_INPUT",key+" 必须是整数");}}
 public static boolean bool(Map<String,?> v,String key,boolean fallback){Object x=v.get(key);return x==null?fallback:Boolean.parseBoolean(x.toString());}
 public static String id(){return UUID.randomUUID().toString();}
}

