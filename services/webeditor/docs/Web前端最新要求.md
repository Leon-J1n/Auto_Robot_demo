1.删减gateway server功能，将加载场景、解析、轨迹文件播放等功能都提取到typescript和three.js进行。

gateway server和网页前端不再提供实时场景编辑功能，如果需要更新场景中的某一部分，会有其他调用方使用POST /updateItem 方法来对场景进行更新。

在加载场景时，有两种方式：a）用户通过点击Load Scene按钮上传一个json文件，然后用户浏览器本地会解析场景文件，去指定源拉取必要的模型数据等，并在用户本地显示。b）其他调用方通过 POST /updateWorld 方法传递场景文件，gateway server收到该请求后告诉浏览器更新场景。

加载场景时，如果需要模型数据等信息，从ObjectLibraryServer获取，ObjectLibraryServer暂未实现完成，但目前接口约定已经确认完成，见OLS接口.md。先在config里留出配置变量，方便后续修改。

加载轨迹时同样有类似的两种方式来加载，分别为用户手动加载和其他调用方通过 POST /updateTraj 方法传递轨迹文件。

最左侧导航栏保留以下功能：1.场景层级显示 2.资产面板 ，其他删除

原有的场景保存点等功能，全部删去。

资产面板进行升级，新的资产面板包括两部分：1.预设场景 2.预制模型 具体信息从ObjectLibraryServer获取。用户点击某个预设场景，浏览器本地将清空原场景，然后加载这个场景。

原有Agent功能暂时不变，但由于预设场景的加入，因此发送的信息中需要新增一个字段。

新增拍照功能，当其他调用方调用GET /captureScene 时，从指定相机渲染图像并发送。

新增绘制包围盒功能，根据指定的物体id，绘制该物体的AABB包围盒。

gateway server不再在服务器端（python侧）维护场景，场景和播放全部交给浏览器端。



接口明细表：

| 类型 | 名称          | 参数        | 说明                                                         |
| ---- | ------------- | ----------- | ------------------------------------------------------------ |
| GET  | /captureScene | id,type     | 指定物体id必须存在且具有RGBCamera或RGBDCamera组件，type=rgb\|rgbd |
| POST | /updateWorld  | world       | 根据新的世界描述文件（json格式）重新加载世界                 |
| POST | /updateItem   | id,newInfo  | 对指定id的物体修改信息                                       |
| POST | /updateTraj   | traj        | 根据指定轨迹文件数据（二进制格式，见F:\web-frontend\轨迹文件布局描述.md）播放轨迹 |
| GET  | /get2Daabb    | id,cameraID | 对于指定物体，获取该物体在指定camera中的2D包围盒             |



