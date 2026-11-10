通信流程

1.启动gateway和Agent服务

2.浏览器向gateway发送任务命令和场景数据（每次发送只发新一轮的命令和场景）

3.gateway向Agent服务调用 POST /startTask，Agent服务在返回数据中填入任务ID

4.gateway每5秒向Agent服务轮询任务状态GET /queryTaskState，任务状态有“已创建”、“已进入队列”、“进行中”、“任务完成”、“任务失败”

5.Agent在任务进行中可以调用gateway的接口（如updateWorld，updateItem，updateTraj等）

6.如果出现任务失败，gateway向Agent调用GET /queryTaskFailureReason

7.结束



Agent服务接口清单：

| 类型 | 名称                    | 参数          | 说明                                                         |
| ---- | ----------------------- | ------------- | ------------------------------------------------------------ |
| GET  | /queryTaskState         | taskID        | 获取指定任务的运行状态，任务状态有“已创建”、“已进入队列”、“进行中”、“任务完成”、“任务失败” |
| POST | /startTask              | command,scene | command是用户新一轮发送的命令，scene是场景（json格式）。返回一个任务ID |
| GET  | /queryTaskFailureReason | taskID        | 查询指定任务的失败原因                                       |

