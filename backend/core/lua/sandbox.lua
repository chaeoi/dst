local mode, folder = arg[1], arg[2] or ""
local source = io.read("*a")
if #source > 2 * 1024 * 1024 then error("配置文件超过 2 MB") end
local env = {
  assert=assert, error=error, ipairs=ipairs, pairs=pairs, next=next,
  tonumber=tonumber, tostring=tostring, type=type, select=select, unpack=unpack,
  math=math, string=string, table=table, locale="zh", folder_name=folder,
  modname=folder, print=function() end,
}
env.string.dump = nil
env._G, env.GLOBAL = env, env
env.ChooseTranslationTable = function(t) if t.zh or t.zhr or t.en then return t.zh or t.zhr or t.en end local _,v=next(t) return v end
local fn, err = loadstring(source, "configuration")
if not fn then error(err) end
setfenv(fn, env)
local ticks = 0
debug.sethook(function()
  ticks = ticks + 1
  if ticks > 2000 then error("配置计算超过限制") end
end, "", 1000)
local ok, value = pcall(fn)
if not ok then error(value) end
if mode == "modinfo" then
  value = {name=env.name, description=env.description, author=env.author,
    version=env.version, client_only_mod=env.client_only_mod,
    configuration_options=env.configuration_options or {}}
end
local function quote(s)
  return '"' .. tostring(s):gsub('[%z\1-\31\\"]', function(c)
    if c == '"' then return '\\"' end
    if c == '\\' then return '\\\\' end
    return string.format('\\u%04x', string.byte(c))
  end) .. '"'
end
local seen = {}
local function encode(v, depth)
  if depth > 40 then error("配置嵌套过深") end
  local kind = type(v)
  if kind == "nil" then return "null" end
  if kind == "boolean" then return v and "true" or "false" end
  if kind == "number" then
    if v ~= v or v == math.huge or v == -math.huge then error("配置含非法数字") end
    return string.format("%.17g", v)
  end
  if kind == "string" then return quote(v) end
  if kind ~= "table" then error("配置含不可编辑的类型：" .. kind) end
  if seen[v] then error("配置含循环引用") end
  seen[v] = true
  local count, array = 0, true
  for k in pairs(v) do
    count = count + 1
    if type(k) ~= "number" or k < 1 or k % 1 ~= 0 then array = false end
  end
  if count ~= #v then array = false end
  local out = {}
  if array and count > 0 then
    for i=1,count do out[#out+1] = encode(v[i], depth+1) end
  else
    for k,x in pairs(v) do
      if type(k) ~= "string" then error("配置字典含非字符串键，保留原文件") end
      out[#out+1] = quote(k) .. ":" .. encode(x, depth+1)
    end
  end
  seen[v] = nil
  return (array and count>0 and "[" or "{") .. table.concat(out, ",") .. (array and count>0 and "]" or "}")
end
local result = encode(value, 0)
if #result > 2 * 1024 * 1024 then error("配置结果超过 2 MB") end
io.write(result)
