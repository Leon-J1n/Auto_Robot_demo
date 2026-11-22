#include "sim/robot.hpp"
#include "sim/render.hpp"
#include "sim/collision.hpp"
#include "sim/ifps.hpp"
#include "sim/assets.hpp"
#include "sim/physics.hpp"
#include "sim/plugin.hpp"
#include "sim/state_io.hpp"
#include "sim/sensors.hpp"
#include "sim/logging.hpp"

#if defined(_MSC_VER) && _MSC_VER >= 1950
#include <cstddef>
// Qt 5.15 references iterators removed from the VS 2026 STL. Raw pointers retain
// the same release-mode behavior and keep this compatibility shim local to the editor.
namespace stdext {
template <typename Value>
Value* make_checked_array_iterator(Value* pointer, std::size_t) { return pointer; }
template <typename Value>
Value* make_unchecked_array_iterator(Value* pointer) { return pointer; }
}  // namespace stdext
#endif

#include <QApplication>
#include <QAbstractItemView>
#include <QActionGroup>
#include <QCheckBox>
#include <QColorDialog>
#include <QComboBox>
#include <QCoreApplication>
#include <QDateTime>
#include <QDockWidget>
#include <QDir>
#include <QDoubleSpinBox>
#include <QDropEvent>
#include <QElapsedTimer>
#include <QFileDialog>
#include <QFile>
#include <QFormLayout>
#include <QHBoxLayout>
#include <QHostAddress>
#include <QLabel>
#include <QInputDialog>
#include <QImage>
#include <QKeyEvent>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLineEdit>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QEventLoop>
#include <QMainWindow>
#include <QMenuBar>
#include <QMessageBox>
#include <QMouseEvent>
#include <QPlainTextEdit>
#include <QPixmap>
#include <QPainter>
#include <QPainterPath>
#include <QPainterPathStroker>
#include <QPushButton>
#include <QSaveFile>
#include <QSizePolicy>
#include <QSpinBox>
#include <QStatusBar>
#include <QTcpServer>
#include <QTreeWidget>
#include <QToolBar>
#include <QTimer>
#include <QTcpSocket>
#include <QUrl>
#include <QVBoxLayout>
#include <QWheelEvent>
#include <QUuid>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <functional>
#include <limits>
#include <optional>
#include <string_view>
#include <unordered_map>
#include <unordered_set>

namespace {

sim::Quaternion EulerDegrees(double roll, double pitch, double yaw) {
    constexpr double radians = 3.14159265358979323846 / 180.0;
    const double cr = std::cos(roll * radians * 0.5), sr = std::sin(roll * radians * 0.5);
    const double cp = std::cos(pitch * radians * 0.5), sp = std::sin(pitch * radians * 0.5);
    const double cy = std::cos(yaw * radians * 0.5), sy = std::sin(yaw * radians * 0.5);
    return {cr * cp * cy + sr * sp * sy, sr * cp * cy - cr * sp * sy,
            cr * sp * cy + sr * cp * sy, cr * cp * sy - sr * sp * cy};
}

sim::Vec3 QuaternionEulerDegrees(const sim::Quaternion& quaternion) {
    constexpr double degrees = 180.0 / 3.14159265358979323846;
    const auto q = quaternion.normalized();
    const double sin_roll = 2.0 * (q.w * q.x + q.y * q.z);
    const double cos_roll = 1.0 - 2.0 * (q.x * q.x + q.y * q.y);
    const double sin_pitch = std::clamp(2.0 * (q.w * q.y - q.z * q.x), -1.0, 1.0);
    const double sin_yaw = 2.0 * (q.w * q.z + q.x * q.y);
    const double cos_yaw = 1.0 - 2.0 * (q.y * q.y + q.z * q.z);
    return {std::atan2(sin_roll, cos_roll) * degrees, std::asin(sin_pitch) * degrees,
            std::atan2(sin_yaw, cos_yaw) * degrees};
}

std::filesystem::path PathFromUtf8(std::string_view value) {
    const auto* begin = reinterpret_cast<const char8_t*>(value.data());
    return std::filesystem::path(std::u8string(begin, begin + value.size()));
}

struct CollisionWireframe {
    std::vector<sim::Vec3> vertices;
    std::vector<std::array<std::uint32_t, 2>> edges;
};

class ViewportLabel final : public QLabel {
public:
    using DragHandler = std::function<void(const QPoint&, const QPoint&, Qt::MouseButtons,
                                           Qt::KeyboardModifiers)>;
    using ClickHandler = std::function<void(const QPoint&)>;
    using WheelHandler = std::function<void(int)>;
    using KeyHandler = std::function<void(int)>;
    using PressHandler = std::function<void(const QPoint&, Qt::MouseButton)>;
    using ReleaseHandler = std::function<void()>;

    explicit ViewportLabel(QWidget* parent = nullptr) : QLabel(parent) {
        setMouseTracking(true);
        setFocusPolicy(Qt::StrongFocus);
    }
    DragHandler dragged;
    ClickHandler clicked;
    WheelHandler wheeled;
    KeyHandler keyed;
    PressHandler pressed;
    ReleaseHandler released;

protected:
    void mousePressEvent(QMouseEvent* event) override {
        setFocus(Qt::MouseFocusReason);
        last_position_ = event->pos();
        pressed_position_ = event->pos();
        drag_started_ = false;
        if (pressed) pressed(event->pos(), event->button());
        QLabel::mousePressEvent(event);
    }
    void mouseMoveEvent(QMouseEvent* event) override {
        const QPoint delta = event->pos() - last_position_;
        last_position_ = event->pos();
        if (!drag_started_ &&
            (event->pos() - pressed_position_).manhattanLength() >= 4)
            drag_started_ = true;
        if (drag_started_ && !delta.isNull() && dragged)
            dragged(delta, event->pos(), event->buttons(), event->modifiers());
        QLabel::mouseMoveEvent(event);
    }
    void mouseReleaseEvent(QMouseEvent* event) override {
        if (!drag_started_ && clicked)
            clicked(event->pos());
        if (released) released();
        QLabel::mouseReleaseEvent(event);
    }
    void wheelEvent(QWheelEvent* event) override {
        if (wheeled) wheeled(event->angleDelta().y());
        event->accept();
    }
    void keyPressEvent(QKeyEvent* event) override {
        if (keyed) keyed(event->key());
        event->accept();
    }

private:
    QPoint last_position_;
    QPoint pressed_position_;
    bool drag_started_{false};
};

class HierarchyTree final : public QTreeWidget {
public:
    using ReparentHandler =
        std::function<bool(sim::EntityId, std::optional<sim::EntityId>)>;

    explicit HierarchyTree(QWidget* parent = nullptr) : QTreeWidget(parent) {
        setSelectionMode(QAbstractItemView::SingleSelection);
        setDragEnabled(true);
        setAcceptDrops(true);
        setDragDropMode(QAbstractItemView::InternalMove);
        setDefaultDropAction(Qt::MoveAction);
        setDropIndicatorShown(true);
        setToolTip("Drag an entity onto another entity to change its parent; drop in empty space to detach it.");
    }

    ReparentHandler reparent_requested;

protected:
    void dropEvent(QDropEvent* event) override {
        const auto selected = selectedItems();
        if (selected.size() != 1 || !reparent_requested) {
            event->ignore();
            return;
        }
        const auto* source = selected.front();
        const auto child = static_cast<sim::EntityId>(
            source->data(0, Qt::UserRole).toULongLong());
        const auto* target = itemAt(event->pos());
        const QTreeWidgetItem* requested_parent = nullptr;
        if (dropIndicatorPosition() == QAbstractItemView::OnItem) {
            requested_parent = target;
        } else if (target != nullptr) {
            requested_parent = target->parent();
        }
        std::optional<sim::EntityId> parent;
        if (requested_parent != nullptr)
            parent = static_cast<sim::EntityId>(
                requested_parent->data(0, Qt::UserRole).toULongLong());
        const auto handler = reparent_requested;
        // QTreeWidget removes the source row after an accepted internal move. Rebuild the
        // hierarchy after this drop event returns so Qt cannot delete an item from the new tree.
        QTimer::singleShot(0, this, [handler, child, parent] {
            (void)handler(child, parent);
        });
        event->setDropAction(Qt::IgnoreAction);
        event->ignore();
    }
};

}  // namespace

enum class TransformTool { Move, Rotate, Scale };

struct ControlOptions {
    bool enabled{false};
    QString host{"127.0.0.1"};
    quint16 port{0};
    QString registry_directory;
    QString instance_name;
    QString instance_id;
    QStringList advertise;
};

class MainWindow final : public QMainWindow {
public:
    explicit MainWindow(bool smoke_test = false, ControlOptions control = {})
        : simulator_(), world_(&simulator_.create_world()), resources_(".sim-cache"),
          control_options_(std::move(control)) {
        sim::InstallRobotControlSystem(*world_);
        create_light_entity("Sky Light", sim::LightType::Directional);
        create_light_entity(QString::fromUtf8("\xE7\x8E\xAF\xE5\xA2\x83\xE5\x85\x89"),
                            sim::LightType::Ambient);
        world_->capture_initial_state();
        setWindowTitle("Simulation Platform Editor");
        resize(1280, 800);

        viewport_ = new ViewportLabel;
#if defined(SIM_HAS_VULKAN)
        viewport_->setText("Initializing Vulkan viewport...");
#else
        viewport_->setText("Vulkan viewport unavailable: configure SIM_ENABLE_VULKAN with a Vulkan SDK");
#endif
        viewport_->setAlignment(Qt::AlignCenter);
        viewport_->setMinimumSize(640, 360);
        viewport_->setStyleSheet("background:#000000;color:#d0d4da;font-size:16px;");
        viewport_->setToolTip("Left drag: orbit; drag a colored gizmo handle to edit | Middle/right drag: pan | Wheel: zoom | F: focus selection | WASDQE: fly");
        setCentralWidget(viewport_);

        auto* toolbar = addToolBar("Simulation");
        auto* load = toolbar->addAction("Load Asset");
        auto* load_remote = toolbar->addAction("Load Remote Asset");
        auto* frame_scene_action = toolbar->addAction("Frame Scene");
        auto* start = toolbar->addAction("Start");
        auto* pause = toolbar->addAction("Pause");
        auto* step = toolbar->addAction("Step");
        auto* reset = toolbar->addAction("Reset");
        auto* save_state = toolbar->addAction("Save State");
        auto* restore_state = toolbar->addAction("Restore State");
        auto* joint_target = toolbar->addAction("Joint Target");
        auto* agent_log = toolbar->addAction("Agent Log");
        auto* connect_agent = toolbar->addAction("Connect Agent");
        toolbar->addSeparator();
        toolbar->addWidget(new QLabel(" Real-time factor: "));
        auto* factor = new QSpinBox;
        factor->setRange(0, 10); factor->setValue(1); toolbar->addWidget(factor);

        auto* transform_toolbar = addToolBar("Transform Tools");
        auto* transform_group = new QActionGroup(this);
        transform_group->setExclusive(true);
        auto* move_tool = transform_toolbar->addAction("Move");
        auto* rotate_tool = transform_toolbar->addAction("Rotate");
        auto* scale_tool = transform_toolbar->addAction("Scale");
        transform_toolbar->addSeparator();
        auto* show_colliders = transform_toolbar->addAction("Show Colliders");
        show_colliders->setCheckable(true);
        show_colliders->setShortcut(Qt::Key_C);
        show_colliders->setToolTip("Toggle collision wireframes (C): green static, cyan dynamic, magenta trigger");
        menuBar()->addMenu("View")->addAction(show_colliders);
        for (auto* action : {move_tool, rotate_tool, scale_tool}) {
            action->setCheckable(true);
            transform_group->addAction(action);
        }
        move_tool->setChecked(true);

        auto* primitive_menu = menuBar()->addMenu("Add Primitive");
        const auto add_primitive_action = [this, primitive_menu](
            const QString& label, sim::ColliderShape shape, const QString& mesh,
            const sim::Vec3& size) {
            auto* action = primitive_menu->addAction(label);
            connect(action, &QAction::triggered, this,
                    [this, label, shape, mesh, size] {
                        create_primitive(label, shape, mesh, size);
                    });
        };
        add_primitive_action("Cube 1m", sim::ColliderShape::Box, {}, {1.0, 1.0, 1.0});
        add_primitive_action("Sphere 1m", sim::ColliderShape::Sphere,
                             "builtin://sphere", {0.5, 0.5, 0.5});
        add_primitive_action("Plane 1m", sim::ColliderShape::Box,
                             "builtin://plane", {1.0, 1.0, 0.02});
        add_primitive_action("Capsule 1m", sim::ColliderShape::Capsule,
                             "builtin://capsule", {0.25, 0.25, 0.5});
        add_primitive_action("Cylinder 1m", sim::ColliderShape::Cylinder,
                             "builtin://cylinder", {0.5, 0.5, 1.0});
        auto* light_menu = menuBar()->addMenu("Add Light");
        for (const auto& item : std::array<std::pair<const char*, sim::LightType>, 3>{
                 std::pair{"Point Light", sim::LightType::Point},
                 std::pair{"Spotlight", sim::LightType::Spot},
                 std::pair{"Directional / Sky Light", sim::LightType::Directional}}) {
            auto* action = light_menu->addAction(item.first);
            connect(action, &QAction::triggered, this, [this, type = item.second] {
                const auto entity = create_light_entity(type == sim::LightType::Point ? "Point Light" :
                    type == sim::LightType::Spot ? "Spotlight" :
                    type == sim::LightType::Directional ? "Directional Light" : "Ambient Light", type);
                world_->capture_initial_state(); refresh_scene(); reveal_entity(entity); render_viewport();
            });
        }
        auto* ambient_action = light_menu->addAction(
            QString::fromUtf8("\xE7\x8E\xAF\xE5\xA2\x83\xE5\x85\x89"));
        connect(ambient_action, &QAction::triggered, this, [this] {
            const auto entity = create_light_entity(
                QString::fromUtf8("\xE7\x8E\xAF\xE5\xA2\x83\xE5\x85\x89"), sim::LightType::Ambient);
            world_->capture_initial_state(); refresh_scene(); reveal_entity(entity); render_viewport();
        });
        auto* sensor_menu = menuBar()->addMenu("Add Sensor");
        auto* add_rgb_camera = sensor_menu->addAction("RGB Camera");
        auto* add_rgbd_camera = sensor_menu->addAction("RGBD Camera");
        move_tool->setToolTip("Move the selected entity with the viewport gizmo");
        rotate_tool->setToolTip("Rotate the selected entity with the viewport gizmo");
        scale_tool->setToolTip("Scale the selected entity with the viewport gizmo");

        hierarchy_ = new HierarchyTree;
        hierarchy_->setHeaderHidden(true);
        auto* hierarchy_dock = new QDockWidget("Scene Hierarchy", this);
        hierarchy_dock->setMinimumWidth(260);
        hierarchy_dock->setWidget(hierarchy_); addDockWidget(Qt::LeftDockWidgetArea, hierarchy_dock);

        auto* inspector_widget = new QWidget;
        auto* inspector_layout = new QFormLayout(inspector_widget);
        name_edit_ = new QLineEdit;
        inspector_layout->addRow("Name", name_edit_);
        const auto add_vector_editor = [inspector_layout](const QString& label,
                                                          std::array<QDoubleSpinBox*, 3>& fields,
                                                          double minimum, double maximum) {
            auto* row = new QWidget;
            auto* layout = new QHBoxLayout(row);
            layout->setContentsMargins(0, 0, 0, 0);
            layout->setSpacing(3);
            for (auto*& field : fields) {
                field = new QDoubleSpinBox;
                field->setDecimals(5);
                field->setRange(minimum, maximum);
                field->setSingleStep(0.01);
                layout->addWidget(field);
            }
            inspector_layout->addRow(label, row);
        };
        add_vector_editor("Position XYZ", position_edit_, -1.0e6, 1.0e6);
        add_vector_editor("Rotation RPY°", rotation_edit_, -360.0, 360.0);
        add_vector_editor("Scale XYZ", scale_edit_, 0.00001, 1.0e6);
        visible_edit_ = new QCheckBox("Visible");
        inspector_layout->addRow("Visual", visible_edit_);
        min_lod_edit_ = new QSpinBox;
        min_lod_edit_->setRange(0, 7);
        min_lod_edit_->setToolTip("Finest LOD allowed: 0 is the original full-detail mesh");
        inspector_layout->addRow("Finest LOD", min_lod_edit_);
        max_lod_edit_ = new QSpinBox;
        max_lod_edit_->setRange(0, 7);
        max_lod_edit_->setValue(7);
        max_lod_edit_->setToolTip("Coarsest LOD allowed: 7 is the lowest-detail mesh");
        inspector_layout->addRow("Coarsest LOD", max_lod_edit_);
        light_type_edit_ = new QComboBox;
        light_type_edit_->addItems({"Point", "Spotlight", "Directional", "Ambient"});
        light_enabled_edit_ = new QCheckBox("Enabled");
        light_intensity_edit_ = new QDoubleSpinBox;
        light_intensity_edit_->setRange(0.0, 1.0e6); light_intensity_edit_->setDecimals(4);
        light_color_edit_ = new QPushButton("#FFFFFF");
        light_color_edit_->setToolTip("Choose the emitted light color");
        light_range_edit_ = new QDoubleSpinBox;
        light_range_edit_->setRange(0.0, 1.0e6); light_range_edit_->setSuffix(" m");
        light_decay_edit_ = new QDoubleSpinBox;
        light_decay_edit_->setRange(0.0, 128.0); light_decay_edit_->setDecimals(3);
        light_inner_cone_edit_ = new QDoubleSpinBox;
        light_inner_cone_edit_->setRange(0.0, 89.9); light_inner_cone_edit_->setSuffix(" deg");
        light_outer_cone_edit_ = new QDoubleSpinBox;
        light_outer_cone_edit_->setRange(0.1, 89.9); light_outer_cone_edit_->setSuffix(" deg");
        light_angular_diameter_edit_ = new QDoubleSpinBox;
        light_angular_diameter_edit_->setRange(0.0, 90.0); light_angular_diameter_edit_->setSuffix(" deg");
        light_widget_ = new QWidget;
        auto* light_layout = new QFormLayout(light_widget_);
        light_layout->addRow("Type", light_type_edit_);
        light_layout->addRow("Enabled", light_enabled_edit_);
        light_layout->addRow("Color", light_color_edit_);
        light_layout->addRow("Intensity", light_intensity_edit_);
        light_layout->addRow("Range", light_range_edit_);
        light_layout->addRow("Decay", light_decay_edit_);
        light_layout->addRow("Inner Cone Angle", light_inner_cone_edit_);
        light_layout->addRow("Outer Cone Angle", light_outer_cone_edit_);
        light_layout->addRow("Angular Diameter", light_angular_diameter_edit_);
        light_widget_->setVisible(false);
        inspector_layout->addRow(light_widget_);
        parent_label_ = new QLabel("-");
        components_label_ = new QLabel("Select an entity to inspect and edit it.");
        components_label_->setWordWrap(true);
        inspector_layout->addRow("Parent", parent_label_);
        inspector_layout->addRow("Components", components_label_);
        auto* delete_entity = new QPushButton("Delete selected entity");
        inspector_layout->addRow(delete_entity);
        inspector_widget->setEnabled(false);
        inspector_widget_ = inspector_widget;
        auto* inspector_dock = new QDockWidget("Inspector", this);
        inspector_dock->setMinimumWidth(410);
        inspector_dock->setWidget(inspector_widget); addDockWidget(Qt::RightDockWidgetArea, inspector_dock);

        physics_widget_ = new QWidget;
        auto* physics_layout = new QFormLayout(physics_widget_);
        body_enabled_edit_ = new QCheckBox("Enabled");
        physics_layout->addRow("Physics Body", body_enabled_edit_);
        body_type_edit_ = new QComboBox;
        body_type_edit_->addItems({"Static", "Dynamic"});
        physics_layout->addRow("Motion", body_type_edit_);
        gravity_edit_ = new QCheckBox("Enabled");
        physics_layout->addRow("Gravity", gravity_edit_);
        mass_edit_ = new QDoubleSpinBox;
        mass_edit_->setDecimals(4);
        mass_edit_->setRange(0.0001, 1.0e9);
        mass_edit_->setValue(1.0);
        physics_layout->addRow("Mass (kg)", mass_edit_);
        const std::array<QString, 3> friction_labels{
            "Sliding Friction", "Torsional Friction", "Rolling Friction"};
        for (std::size_t index = 0; index < friction_edit_.size(); ++index) {
            friction_edit_[index] = new QDoubleSpinBox;
            friction_edit_[index]->setDecimals(6);
            friction_edit_[index]->setRange(0.0, 1.0e6);
            friction_edit_[index]->setSingleStep(index == 0 ? 0.05 : 0.001);
            physics_layout->addRow(friction_labels[index], friction_edit_[index]);
        }
        restitution_edit_ = new QDoubleSpinBox;
        restitution_edit_->setDecimals(3);
        restitution_edit_->setRange(0.0, 1.0);
        restitution_edit_->setSingleStep(0.05);
        restitution_edit_->setToolTip("0 is non-bouncy; 1 is approximately energy-preserving");
        physics_layout->addRow("Restitution", restitution_edit_);
        physics_widget_->setToolTip(
            "Contact parameters require a Collider. Dynamic bodies require an active physics backend.");
        physics_widget_->setEnabled(false);
        auto* physics_dock = new QDockWidget("Physics", this);
        physics_dock->setMinimumWidth(410);
        physics_dock->setWidget(physics_widget_);
        addDockWidget(Qt::RightDockWidgetArea, physics_dock);

        ifps_widget_ = new QWidget;
        auto* ifps_layout = new QFormLayout(ifps_widget_);
        ifps_show_edit_ = new QCheckBox("Visible for selected object");
        ifps_show_edit_->setChecked(false);
        ifps_layout->addRow("Shell", ifps_show_edit_);
        ifps_count_edit_ = new QSpinBox;
        ifps_count_edit_->setRange(1, 1'000'000);
        ifps_count_edit_->setValue(256);
        ifps_count_edit_->setToolTip(
            "Positive leaf-sphere count. Large values significantly increase offline build time.");
        ifps_layout->addRow("Leaf Spheres", ifps_count_edit_);
        ifps_mode_edit_ = new QComboBox;
        ifps_mode_edit_->addItems({"Wireframe", "Solid"});
        ifps_layout->addRow("Display", ifps_mode_edit_);
        auto* ifps_layers = new QWidget;
        auto* ifps_layers_layout = new QVBoxLayout(ifps_layers);
        ifps_layers_layout->setContentsMargins(0, 0, 0, 0);
        ifps_layers_layout->setSpacing(2);
        const std::array<QColor, 8> ifps_colors{{QColor(240, 75, 75), QColor(80, 215, 120),
            QColor(45, 190, 245), QColor(255, 190, 55), QColor(220, 90, 235),
            QColor(70, 225, 205), QColor(255, 125, 45), QColor(150, 135, 255)}};
        for (std::size_t index = 0; index < ifps_level_edits_.size(); ++index) {
            auto* level = new QCheckBox(QString("Level %1").arg(index));
            level->setStyleSheet(QString("QCheckBox { color: rgb(%1,%2,%3); }")
                .arg(ifps_colors[index].red()).arg(ifps_colors[index].green())
                .arg(ifps_colors[index].blue()));
            ifps_level_edits_[index] = level;
            ifps_layers_layout->addWidget(level);
        }
        ifps_layout->addRow("Levels", ifps_layers);
        ifps_widget_->setEnabled(false);
        auto* ifps_dock = new QDockWidget("IFPS Visualization", this);
        ifps_dock->setMinimumWidth(410);
        ifps_dock->setWidget(ifps_widget_);
        addDockWidget(Qt::RightDockWidgetArea, ifps_dock);

        camera_widget_ = new QWidget;
        auto* camera_layout = new QFormLayout(camera_widget_);
        camera_type_edit_ = new QComboBox;
        camera_type_edit_->addItems({"RGB", "RGBD"});
        camera_layout->addRow("Output", camera_type_edit_);
        camera_frame_edit_ = new QLineEdit;
        camera_layout->addRow("Frame ID", camera_frame_edit_);
        camera_rate_edit_ = new QDoubleSpinBox;
        camera_rate_edit_->setRange(0.1, 1000.0);
        camera_rate_edit_->setValue(30.0);
        camera_rate_edit_->setSuffix(" Hz");
        camera_layout->addRow("Update Rate", camera_rate_edit_);
        camera_width_edit_ = new QSpinBox;
        camera_width_edit_->setRange(1, 8192);
        camera_width_edit_->setValue(640);
        camera_layout->addRow("Width", camera_width_edit_);
        camera_height_edit_ = new QSpinBox;
        camera_height_edit_->setRange(1, 8192);
        camera_height_edit_->setValue(480);
        camera_layout->addRow("Height", camera_height_edit_);
        camera_fov_edit_ = new QDoubleSpinBox;
        camera_fov_edit_->setRange(1.0, 179.0);
        camera_fov_edit_->setValue(60.0);
        camera_fov_edit_->setSuffix(" deg");
        camera_layout->addRow("Vertical FOV", camera_fov_edit_);
        camera_near_edit_ = new QDoubleSpinBox;
        camera_near_edit_->setDecimals(4);
        camera_near_edit_->setRange(0.0001, 1.0e5);
        camera_near_edit_->setValue(0.1);
        camera_near_edit_->setSuffix(" m");
        camera_layout->addRow("Near Plane", camera_near_edit_);
        camera_far_edit_ = new QDoubleSpinBox;
        camera_far_edit_->setDecimals(2);
        camera_far_edit_->setRange(0.001, 1.0e7);
        camera_far_edit_->setValue(10.0);
        camera_far_edit_->setSuffix(" m");
        camera_layout->addRow("Far Plane", camera_far_edit_);
        camera_preview_edit_ = new QComboBox;
        camera_preview_edit_->addItems({"RGB", "Depth"});
        camera_layout->addRow("Preview", camera_preview_edit_);
        camera_widget_->setEnabled(false);
        camera_widget_->setToolTip("Camera optical axis is the entity's local +X direction.");
        auto* camera_dock = new QDockWidget("Camera Properties", this);
        camera_dock->setMinimumWidth(410);
        camera_dock->setWidget(camera_widget_);
        addDockWidget(Qt::RightDockWidgetArea, camera_dock);

        log_ = new QPlainTextEdit; log_->setReadOnly(true);
        log_->appendPlainText("Backend configuration is being initialized...");
        auto* log_dock = new QDockWidget("Log", this);
        log_dock->setWidget(log_); addDockWidget(Qt::BottomDockWidgetArea, log_dock);

        agent_log_ = new QPlainTextEdit; agent_log_->setReadOnly(true);
        agent_log_->setPlaceholderText("Load an Agent JSONL command log to inspect scene operations.");
        auto* agent_dock = new QDockWidget("Agent Operations", this);
        agent_dock->setWidget(agent_log_); addDockWidget(Qt::BottomDockWidgetArea, agent_dock);

        sensor_view_ = new QLabel("Sensor display unavailable until a render/collision provider is active.");
        sensor_view_->setAlignment(Qt::AlignCenter);
        sensor_view_->setMinimumSize(0, 0);
        sensor_view_->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Ignored);
        auto* sensor_dock = new QDockWidget("Sensor Frames", this);
        sensor_dock->setWidget(sensor_view_); addDockWidget(Qt::RightDockWidgetArea, sensor_dock);

        auto* backend_widget = new QWidget;
        auto* backend_layout = new QFormLayout(backend_widget);
        physics_backend_ = new QComboBox;
        physics_backend_->addItems({"None", "MuJoCo"});
#if defined(SIM_HAS_BULLET_PLUGIN)
        physics_backend_->addItem("Bullet3");
#endif
        collision_backend_ = new QComboBox;
        collision_backend_->addItems({"None", "FCL"});
#if defined(SIM_HAS_BULLET_COLLISION_PLUGIN)
        collision_backend_->addItem("Bullet3");
#endif
#if defined(SIM_HAS_COAL_COLLISION_PLUGIN)
        collision_backend_->addItem("Coal");
#endif
        collision_backend_->addItems({"IFPS-CPU", "IFPS-GPU"});
        collision_backend_->setCurrentText("FCL");
        render_backend_ = new QComboBox;
        render_backend_->addItems({"None", "Vulkan"});
        render_backend_->setCurrentText("Vulkan");
        auto* apply_backends = new QPushButton("Apply Backend Configuration");
        backend_status_ = new QLabel("Not applied");
        backend_status_->setWordWrap(true);
        backend_layout->addRow("Physics", physics_backend_);
        backend_layout->addRow("Collision", collision_backend_);
        backend_layout->addRow("Renderer", render_backend_);
        backend_layout->addRow(apply_backends);
        backend_layout->addRow("Active", backend_status_);
        auto* backend_dock = new QDockWidget("Backends", this);
        backend_dock->setWidget(backend_widget);
        addDockWidget(Qt::RightDockWidgetArea, backend_dock);

        tabifyDockWidget(inspector_dock, physics_dock);
        tabifyDockWidget(inspector_dock, ifps_dock);
        tabifyDockWidget(inspector_dock, camera_dock);
        tabifyDockWidget(inspector_dock, sensor_dock);
        tabifyDockWidget(inspector_dock, backend_dock);
        ifps_dock->raise();

        connect(load, &QAction::triggered, this, [this] { load_asset(); });
        connect(load_remote, &QAction::triggered, this, [this] { load_asset_remote_dialog(); });
        connect(add_rgb_camera, &QAction::triggered, this, [this] {
            create_camera_sensor(sim::CameraOutput::RGB);
        });
        connect(add_rgbd_camera, &QAction::triggered, this, [this] {
            create_camera_sensor(sim::CameraOutput::RGBD);
        });
        connect(frame_scene_action, &QAction::triggered, this, [this] { frame_scene(); });
        connect(start, &QAction::triggered, this, [this] { world_->start(); log("Simulation started"); });
        connect(pause, &QAction::triggered, this, [this] { world_->pause(); log("Simulation paused"); });
        connect(step, &QAction::triggered, this, [this] {
            step_simulation(); render_dirty_ = true; render_viewport(); update_status();
        });
        connect(reset, &QAction::triggered, this, [this] {
            world_->reset(); refresh_scene(); render_dirty_ = true; render_viewport(); update_status();
        });
        connect(save_state, &QAction::triggered, this, [this] { save_state_file(); });
        connect(restore_state, &QAction::triggered, this, [this] { restore_state_file(); });
        connect(joint_target, &QAction::triggered, this, [this] { command_joint_target(); });
        connect(agent_log, &QAction::triggered, this, [this] { load_agent_log(); });
        connect(connect_agent, &QAction::triggered, this, [this] { connect_agent_dialog(); });
        connect(factor, qOverload<int>(&QSpinBox::valueChanged), this, [this](int value) {
            gui_real_time_factor_ = static_cast<double>(value);
            world_->time().set_real_time_factor(static_cast<double>(value));
        });
        connect(hierarchy_, &QTreeWidget::currentItemChanged, this,
                [this](QTreeWidgetItem* current, QTreeWidgetItem*) {
                    inspect(current);
                    render_dirty_ = true;
                    render_viewport();
                });
        hierarchy_->reparent_requested = [this](sim::EntityId child,
                                                 std::optional<sim::EntityId> parent) {
            return reparent_entity(child, parent.value_or(sim::kInvalidEntity), true);
        };
        connect(name_edit_, &QLineEdit::editingFinished, this, [this] { apply_inspector(); });
        for (auto* field : position_edit_)
            connect(field, &QDoubleSpinBox::editingFinished, this, [this] { apply_inspector(); });
        for (auto* field : rotation_edit_)
            connect(field, &QDoubleSpinBox::editingFinished, this, [this] { apply_inspector(); });
        for (auto* field : scale_edit_)
            connect(field, &QDoubleSpinBox::editingFinished, this, [this] { apply_inspector(); });
        connect(visible_edit_, &QCheckBox::toggled, this, [this](bool) { apply_inspector(); });
        connect(min_lod_edit_, qOverload<int>(&QSpinBox::valueChanged), this,
                [this](int) { apply_inspector(); });
        connect(max_lod_edit_, qOverload<int>(&QSpinBox::valueChanged), this,
                [this](int) { apply_inspector(); });
        connect(light_type_edit_, qOverload<int>(&QComboBox::currentIndexChanged), this,
                [this](int) {
                    if (inspector_updating_) return;
                    apply_inspector(); inspect(hierarchy_->currentItem());
                });
        connect(light_enabled_edit_, &QCheckBox::toggled, this, [this](bool) { apply_inspector(); });
        connect(light_intensity_edit_, &QDoubleSpinBox::editingFinished, this, [this] { apply_inspector(); });
        connect(light_color_edit_, &QPushButton::clicked, this, [this] {
            const QColor selected = QColorDialog::getColor(light_color_, this, "Light Color");
            if (!selected.isValid()) return;
            light_color_ = selected;
            apply_inspector();
            inspect(hierarchy_->currentItem());
        });
        for (auto* field : {light_range_edit_, light_decay_edit_, light_inner_cone_edit_,
                            light_outer_cone_edit_, light_angular_diameter_edit_})
            connect(field, &QDoubleSpinBox::editingFinished, this, [this] { apply_inspector(); });
        connect(body_enabled_edit_, &QCheckBox::toggled, this,
                [this](bool) { apply_rigid_body_inspector(); });
        connect(body_type_edit_, qOverload<int>(&QComboBox::currentIndexChanged), this,
                [this](int) { apply_rigid_body_inspector(); });
        connect(gravity_edit_, &QCheckBox::toggled, this,
                [this](bool) { apply_rigid_body_inspector(); });
        connect(mass_edit_, &QDoubleSpinBox::editingFinished, this,
                [this] { apply_rigid_body_inspector(); });
        for (auto* field : friction_edit_)
            connect(field, &QDoubleSpinBox::editingFinished, this,
                    [this] { apply_rigid_body_inspector(); });
        connect(restitution_edit_, &QDoubleSpinBox::editingFinished, this,
                [this] { apply_rigid_body_inspector(); });
        connect(ifps_show_edit_, &QCheckBox::toggled, this, [this](bool) {
            if (!inspector_updating_) render_viewport();
        });
        connect(ifps_count_edit_, &QSpinBox::editingFinished, this,
                [this] { apply_ifps_inspector(); });
        connect(ifps_mode_edit_, qOverload<int>(&QComboBox::currentIndexChanged), this,
                [this](int) { if (!inspector_updating_) render_viewport(); });
        for (auto* level : ifps_level_edits_)
            connect(level, &QCheckBox::toggled, this, [this](bool) {
                if (inspector_updating_) return;
                save_ifps_level_mask();
                render_viewport();
            });
        connect(camera_type_edit_, qOverload<int>(&QComboBox::currentIndexChanged), this,
                [this](int) { apply_camera_inspector(); });
        connect(camera_frame_edit_, &QLineEdit::editingFinished, this,
                [this] { apply_camera_inspector(); });
        connect(camera_rate_edit_, &QDoubleSpinBox::editingFinished, this,
                [this] { apply_camera_inspector(); });
        connect(camera_width_edit_, &QSpinBox::editingFinished, this,
                [this] { apply_camera_inspector(); });
        connect(camera_height_edit_, &QSpinBox::editingFinished, this,
                [this] { apply_camera_inspector(); });
        connect(camera_fov_edit_, &QDoubleSpinBox::editingFinished, this,
                [this] { apply_camera_inspector(); });
        connect(camera_near_edit_, &QDoubleSpinBox::editingFinished, this,
                [this] { apply_camera_inspector(); });
        connect(camera_far_edit_, &QDoubleSpinBox::editingFinished, this,
                [this] { apply_camera_inspector(); });
        connect(camera_preview_edit_, qOverload<int>(&QComboBox::currentIndexChanged), this,
                [this](int) { render_sensor_preview(); });
        connect(delete_entity, &QPushButton::clicked, this, [this] { delete_selected_entity(); });
        connect(apply_backends, &QPushButton::clicked, this,
                [this] { apply_backend_configuration(true); });
        connect(move_tool, &QAction::triggered, this, [this] {
            transform_tool_ = TransformTool::Move; render_viewport();
        });
        connect(rotate_tool, &QAction::triggered, this, [this] {
            transform_tool_ = TransformTool::Rotate; render_viewport();
        });
        connect(scale_tool, &QAction::triggered, this, [this] {
            transform_tool_ = TransformTool::Scale; render_viewport();
        });
        connect(show_colliders, &QAction::toggled, this, [this](bool enabled) {
            show_colliders_ = enabled; render_viewport();
        });
        viewport_->pressed = [this](const QPoint& position, Qt::MouseButton button) {
            const QPoint local = position - viewport_pixmap_offset_;
            gizmo_drag_active_ = button == Qt::LeftButton && gizmo_visible_ &&
                                 gizmo_hit_path_.contains(local);
        };
        viewport_->released = [this] {
            if (gizmo_transform_changed_) {
                rebuild_active_backends();
                world_->capture_initial_state();
                gizmo_transform_changed_ = false;
            }
            gizmo_drag_active_ = false;
        };
        viewport_->dragged = [this](const QPoint& delta, const QPoint& position,
                                    Qt::MouseButtons buttons,
                                    Qt::KeyboardModifiers modifiers) {
            manipulate_viewport(delta, position, buttons, modifiers);
        };
        viewport_->wheeled = [this](int delta) {
            camera_distance_ = std::clamp(camera_distance_ * std::exp(-delta / 1200.0),
                                          0.001, 1.0e7);
            render_viewport();
        };
        viewport_->clicked = [this](const QPoint& position) { pick_entity(position); };
        viewport_->keyed = [this](int key) { handle_viewport_key(key); };
        agent_socket_ = new QTcpSocket(this);
        agent_poll_ = new QTimer(this); agent_poll_->setInterval(500);
        connect(agent_poll_, &QTimer::timeout, this, [this] { query_agent_scene(); });
        connect(agent_socket_, &QTcpSocket::connected, this, [this] {
            agent_log_->appendPlainText("Connected to Agent RPC.");
            agent_poll_->start(); query_agent_scene();
        });
        connect(agent_socket_, &QTcpSocket::readyRead, this, [this] { process_agent_responses(); });
        connect(agent_socket_, &QTcpSocket::errorOccurred, this, [this](QAbstractSocket::SocketError) {
            agent_log_->appendPlainText("Agent RPC error: " + agent_socket_->errorString());
        });
        const QString endpoint = qEnvironmentVariable("SIM_EDITOR_AGENT_ENDPOINT");
        if (!endpoint.isEmpty()) connect_agent_endpoint(endpoint);
        if (control_options_.enabled) start_control_server();
        apply_backend_configuration(false);
        const QString initial_asset = qEnvironmentVariable("SIM_EDITOR_INITIAL_ASSET");
        if (!initial_asset.isEmpty()) load_asset_path(initial_asset, false);
        const QString initial_state = qEnvironmentVariable("SIM_EDITOR_INITIAL_STATE");
        if (!initial_state.isEmpty()) {
            try {
                world_->restore(sim::LoadStateFile(initial_state.toStdWString()).world);
                rebuild_active_backends();
                refresh_scene();
                frame_scene();
                render_dirty_ = true;
                log("Restored initial state: " + initial_state);
            } catch (const std::exception& error) {
                log(QString("Initial state error: %1").arg(QString::fromUtf8(error.what())));
            }
        }
        const QString screenshot = qEnvironmentVariable("SIM_EDITOR_SCREENSHOT");
        if (!screenshot.isEmpty()) {
            bool valid_delay = false;
            const int requested_delay =
                qEnvironmentVariable("SIM_EDITOR_SCREENSHOT_DELAY_MS").toInt(&valid_delay);
            const int screenshot_delay = valid_delay ? std::max(100, requested_delay) : 1200;
            QTimer::singleShot(screenshot_delay, this, [this, screenshot] {
                render_viewport();
                if (!grab().save(screenshot))
                    log("Failed to save editor screenshot: " + screenshot);
                QApplication::quit();
            });
        }
        if (smoke_test) {
            render_viewport();
        } else {
            frame_timer_ = new QTimer(this);
            // A precise timer, because the default coarse one is quantised to the platform's
            // tick -- 15.6 ms on Windows, which rounds any interval up to a multiple of
            // itself. The previous 33 ms therefore fired every 46.8 ms and pinned the editor
            // at 21 fps regardless of how little work a frame did, which reads as a slow
            // renderer when it is really a slow clock.
            frame_timer_->setTimerType(Qt::PreciseTimer);
            frame_timer_->setInterval(16);
            connect(frame_timer_, &QTimer::timeout, this, [this] {
                advance_running_simulation();
                if (scene_signature() != last_scene_signature_) render_dirty_ = true;
                if (render_dirty_) render_viewport();
                refresh_dynamic_inspector();
                update_performance_metrics();
                update_status();
            });
            frame_clock_.start();
            performance_clock_.start();
            frame_timer_->start();
        }
        refresh_scene();
        update_status();
    }

    ~MainWindow() override {
        if (!control_descriptor_path_.isEmpty()) QFile::remove(control_descriptor_path_);
    }

private:
    sim::EntityId create_light_entity(const QString& label, sim::LightType type) {
        const auto parent = selected_entity().value_or(sim::kInvalidEntity);
        const auto entity = world_->create_entity(label.toStdString(), parent);
        auto& transform = world_->registry().get<sim::TransformComponent>(entity).local;
        if (parent == sim::kInvalidEntity) transform.translation = camera_target_;
        if (type == sim::LightType::Directional)
            transform.rotation = EulerDegrees(0.0, 90.0, 0.0);
        sim::LightComponent light;
        light.type = type;
        light.intensity = type == sim::LightType::Directional ? 1.0F :
            type == sim::LightType::Ambient ? 0.25F : 4.0F;
        if (type == sim::LightType::Ambient)
            SIM_LOG_DEBUG("Editor.Light",
                          "created ambient light intensity=" + std::to_string(light.intensity));
        light.angular_diameter_radians = type == sim::LightType::Directional ? 0.05F : 0.0F;
        world_->registry().emplace<sim::LightComponent>(entity, light);
        world_->update_world_transforms();
        return entity;
    }

    sim::EntityId create_primitive(const QString& label, sim::ColliderShape shape,
                                   const QString& mesh_resource,
                                   const sim::Vec3& collider_size) {
        const auto entity = world_->create_entity(label.toStdString());
        auto& transform = world_->registry().get<sim::TransformComponent>(entity).local;
        transform.translation = camera_target_;
        const sim::Vec3 visual_size = mesh_resource.isEmpty()
            ? collider_size : sim::Vec3{1.0, 1.0, 1.0};
        world_->registry().emplace<sim::VisualComponent>(entity, sim::VisualComponent{
            mesh_resource.toStdString(), {}, true, shape, visual_size,
            {0.62F, 0.68F, 0.76F, 1.0F}});
        world_->registry().emplace<sim::ColliderComponent>(entity, sim::ColliderComponent{
            shape, collider_size, {}, false});
        world_->registry().emplace<sim::RigidBodyComponent>(entity,
            sim::RigidBodyComponent{1.0, {}, {}, false});
        world_->update_world_transforms();
        rebuild_active_backends();
        world_->capture_initial_state();
        refresh_scene();
        if (entity_items_.contains(entity)) hierarchy_->setCurrentItem(entity_items_.at(entity));
        render_dirty_ = true;
        render_viewport();
        log(QString("Created %1 with matching visual and collision geometry").arg(label));
        return entity;
    }

    /// Draw a trajectory as a row of coloured marker spheres under one container entity,
    /// replacing any earlier trajectory of the same name.
    ///
    /// A marker carries a VisualComponent and deliberately nothing else. A collider would
    /// make the drawn path obstruct the very arm whose motion it depicts, and a rigid body
    /// would let it fall to the floor, so markers stay invisible to physics and to
    /// collision queries: displaying a trajectory leaves the planning scene untouched.
    /// That is also why no backend rebuild follows -- there is no new physics or collision
    /// geometry for the backends to learn about.
    std::size_t draw_trajectory_markers(const std::string& name,
                                        const std::vector<sim::Vec3>& points,
                                        const std::array<float, 4>& color, double radius) {
        clear_trajectory_markers(name);
        if (points.empty()) return 0;
        const auto container = world_->create_entity("__trajectory_" + name);
        // builtin://sphere is authored at radius 0.5, and the renderer ignores
        // VisualComponent::size for an imported mesh, so a marker is sized by its scale.
        const double scale = std::max(radius, 1.0e-4) * 2.0;
        for (const auto& point : points) {
            const auto marker = world_->create_entity("waypoint", container);
            auto& transform = world_->registry().get<sim::TransformComponent>(marker).local;
            transform.translation = point;
            transform.scale = {scale, scale, scale};
            world_->registry().emplace<sim::VisualComponent>(marker, sim::VisualComponent{
                "builtin://sphere", {}, true, sim::ColliderShape::Sphere,
                {1.0, 1.0, 1.0}, color});
        }
        control_trajectories_[name] = container;
        world_->update_world_transforms();
        return points.size();
    }

    /// Erase one named trajectory, or every trajectory when `name` is empty. Returns the
    /// number of trajectories removed. Destroying a container recurses into its markers.
    std::size_t clear_trajectory_markers(const std::string& name) {
        std::size_t removed = 0;
        for (auto it = control_trajectories_.begin(); it != control_trajectories_.end();) {
            if (!name.empty() && it->first != name) {
                ++it;
                continue;
            }
            if (world_->registry().valid(it->second) && world_->destroy_entity(it->second))
                ++removed;
            it = control_trajectories_.erase(it);
        }
        return removed;
    }

    /// The robot a control request names, or the only robot in the scene.
    ///
    /// A caller that has just restored a simstate holds no entity ids of its own, so
    /// demanding one would force it to walk the hierarchy to find what is nearly always
    /// the single robot. Several robots make the choice ambiguous, and it says so rather
    /// than guessing.
    [[nodiscard]] sim::EntityId control_resolve_robot(const QJsonObject& params) const {
        if (params.value("robot_object").isObject())
            return control_resolve_object(params.value("robot_object"));
        if (params.contains("robot"))
            return static_cast<sim::EntityId>(params.value("robot").toDouble());
        std::vector<sim::EntityId> robots;
        for (const auto entity : world_->registry().view<sim::RobotComponent>())
            robots.push_back(entity);
        if (robots.empty()) throw std::runtime_error("scene contains no robot");
        if (robots.size() > 1)
            throw std::runtime_error("scene holds several robots; name one explicitly");
        return robots.front();
    }

    sim::EntityId create_camera_sensor(
        sim::CameraOutput output, std::optional<sim::EntityId> parent_override = std::nullopt) {
        const auto parent = parent_override.value_or(
            selected_entity().value_or(sim::kInvalidEntity));
        const QString label = output == sim::CameraOutput::RGB ? "RGB Camera" : "RGBD Camera";
        const auto entity = world_->create_entity(label.toStdString(), parent);
        auto& transform = world_->registry().get<sim::TransformComponent>(entity).local;
        if (parent == sim::kInvalidEntity) transform.translation = camera_target_;
        world_->registry().emplace<sim::SensorComponent>(entity, sim::SensorComponent{
            output == sim::CameraOutput::RGB ? "rgb_camera_optical_frame"
                                              : "rgbd_camera_optical_frame",
            30.0, world_->time().state().simulation_time});
        sim::CameraComponent camera;
        camera.near_plane = 0.1;
        camera.far_plane = 10.0;
        camera.output = output;
        world_->registry().emplace<sim::CameraComponent>(entity, camera);
        world_->update_world_transforms();
        world_->capture_initial_state();
        refresh_scene();
        reveal_entity(entity);
        render_dirty_ = true;
        render_viewport();
        log(QString("Created %1 sensor [%2]; local +X is the optical axis")
                .arg(label).arg(entity));
        return entity;
    }

    struct StepTimings {
        double physics_ms{0.0};
        double collision_sync_ms{0.0};
    };

    StepTimings step_simulation(std::uint64_t count = 1, bool paced = true) {
        const auto physics_begin = std::chrono::steady_clock::now();
        const double saved_factor = world_->time().state().real_time_factor;
        if (!paced) world_->time().set_real_time_factor(0.0);
        for (std::uint64_t index = 0; index < count; ++index) {
            world_->step();
            if (physics_) physics_->step(*world_);
        }
        if (!paced) world_->time().set_real_time_factor(saved_factor);
        const auto physics_end = std::chrono::steady_clock::now();
        const auto sync_begin = physics_end;
        if (collision_) collision_->sync_from_scene(*world_);
        const auto sync_end = std::chrono::steady_clock::now();
        return {
            std::chrono::duration<double, std::milli>(physics_end - physics_begin).count(),
            std::chrono::duration<double, std::milli>(sync_end - sync_begin).count()};
    }

    void advance_running_simulation() {
        const double elapsed = frame_clock_.isValid()
            ? std::clamp(frame_clock_.restart() / 1000.0, 0.0, 0.25)
            : 0.0;
        if (world_->status() != sim::SimulationStatus::Running || gui_real_time_factor_ <= 0.0) {
            step_accumulator_ = 0.0;
            return;
        }
        const double time_step = world_->time().state().fixed_time_step;
        step_accumulator_ += elapsed * gui_real_time_factor_;
        const auto steps = static_cast<std::uint64_t>(step_accumulator_ / time_step);
        if (steps == 0) return;
        step_simulation(steps, false);
        step_accumulator_ -= static_cast<double>(steps) * time_step;
    }

    void apply_backend_configuration(bool show_error) {
        try {
            sim::BackendConfig configuration;
            configuration.physics = physics_backend_->currentText() == "MuJoCo"
                ? sim::BackendKind::MuJoCo
                : physics_backend_->currentText() == "Bullet3"
                    ? sim::BackendKind::Bullet3 : sim::BackendKind::None;
            configuration.collision = collision_backend_->currentText() == "FCL"
                ? sim::BackendKind::FCL
                : collision_backend_->currentText() == "Bullet3"
                    ? sim::BackendKind::Bullet3Collision
                : collision_backend_->currentText() == "IFPS-CPU"
                    ? sim::BackendKind::IFPSCPU
                : collision_backend_->currentText() == "IFPS-GPU"
                    ? sim::BackendKind::IFPSGPU
                    : collision_backend_->currentText() == "Coal"
                        ? sim::BackendKind::Coal : sim::BackendKind::None;
            configuration.render = render_backend_->currentText() == "Vulkan"
                ? sim::BackendKind::Vulkan : sim::BackendKind::None;
            for (const auto& diagnostic : sim::BackendManager::validate(configuration))
                if (diagnostic.severity == sim::Severity::Fatal)
                    throw std::runtime_error(diagnostic.message);

#if defined(SIM_HAS_MUJOCO)
            if (configuration.physics == sim::BackendKind::MuJoCo) {
                physics_ = std::make_shared<sim::MuJoCoBackend>();
            }
#else
            if (configuration.physics == sim::BackendKind::MuJoCo)
                throw std::runtime_error("MuJoCo is unavailable in this build");
#endif
#if defined(SIM_HAS_BULLET_PLUGIN)
            if (configuration.physics == sim::BackendKind::Bullet3) {
                const char* configured = std::getenv("SIM_PLUGIN_DIR");
                const std::filesystem::path directory =
                    configured != nullptr && *configured != '\0'
                        ? PathFromUtf8(configured)
                        : std::filesystem::path(SIM_DEFAULT_PLUGIN_DIR);
                plugins_.discover(directory);
                physics_ = plugins_.create_physics_backend("sim.physics.bullet3");
            }
#else
            if (configuration.physics == sim::BackendKind::Bullet3)
                throw std::runtime_error("Bullet3 is unavailable in this build");
#endif
            if (configuration.physics == sim::BackendKind::None) physics_.reset();
            if (physics_) physics_->build(*world_);
            if (configuration.collision == sim::BackendKind::FCL) {
#if defined(SIM_HAS_FCL)
                collision_ = std::make_shared<sim::FCLBackend>();
#else
                throw std::runtime_error("FCL is unavailable in this build");
#endif
            } else if (configuration.collision == sim::BackendKind::Bullet3Collision ||
                       configuration.collision == sim::BackendKind::Coal ||
                       configuration.collision == sim::BackendKind::IFPSCPU ||
                       configuration.collision == sim::BackendKind::IFPSGPU) {
                const char* configured = std::getenv("SIM_PLUGIN_DIR");
                const std::filesystem::path directory = configured != nullptr && *configured != '\0'
                    ? PathFromUtf8(configured) : std::filesystem::path(SIM_DEFAULT_PLUGIN_DIR);
                plugins_.discover(directory);
                collision_ = plugins_.create_collision_backend(
                    configuration.collision == sim::BackendKind::Bullet3Collision
                        ? "sim.collision.bullet3"
                        : configuration.collision == sim::BackendKind::Coal
                            ? "sim.collision.coal"
                        : configuration.collision == sim::BackendKind::IFPSCPU
                            ? "sim.collision.ifps-cpu" : "sim.collision.ifps-gpu");
            } else {
                collision_.reset();
            }
            if (collision_) collision_->build(*world_);
#if defined(SIM_HAS_VULKAN)
            if (configuration.render == sim::BackendKind::Vulkan) {
                if (!renderer_) renderer_ = std::make_unique<sim::VulkanBackend>();
                if (!sensor_renderer_)
                    sensor_renderer_ = std::make_unique<sim::VulkanBackend>();
                sensor_system_.reset();
                auto provider = std::shared_ptr<sim::ISensorDataProvider>(
                    sensor_renderer_.get(), [](sim::ISensorDataProvider*) {});
                sensor_system_ = std::make_unique<sim::SensorSystem>(*world_, std::move(provider));
            } else {
                sensor_system_.reset();
                sensor_renderer_.reset();
                renderer_.reset();
                viewport_->clear();
                viewport_->setText("Renderer disabled in Backend Configuration");
                sensor_view_->clear();
                sensor_view_->setText("Renderer disabled");
            }
#else
            if (configuration.render == sim::BackendKind::Vulkan)
                throw std::runtime_error("Vulkan is unavailable in this build");
#endif
            backend_configuration_ = configuration;
            const QString active = QString("physics=%1, collision=%2, render=%3")
                .arg(physics_backend_->currentText(), collision_backend_->currentText(),
                     render_backend_->currentText());
            backend_status_->setText(active);
            log("Backend configuration applied: " + active);
#if defined(SIM_HAS_VULKAN)
            if (renderer_)
                log(QString("Vulkan device: %1").arg(QString::fromStdString(renderer_->device_name())));
#endif
            render_dirty_ = true;
            render_viewport();
        } catch (const std::exception& error) {
            const QString message = QString::fromUtf8(error.what());
            backend_status_->setText("Error: " + message);
            log("Backend configuration failed: " + message);
            if (show_error) QMessageBox::critical(this, "Backend Configuration", message);
        }
    }

    void rebuild_active_backends() {
        try {
            if (physics_) physics_->build(*world_);
            if (collision_) collision_->build(*world_);
        } catch (const std::exception& error) {
            log(QString("Backend rebuild failed: %1").arg(QString::fromUtf8(error.what())));
        }
    }

    void load_asset() {
        const QString path = QFileDialog::getOpenFileName(this, "Load Asset", {},
                                                          "Assets (*.urdf *.obj *.fbx *.stl)");
        if (path.isEmpty()) return;
        load_asset_path(path, true);
    }

    void load_asset_remote_dialog() {
        bool ok = false;
        const QString url = QInputDialog::getText(
            this, "Load Remote Asset", "URL:", QLineEdit::Normal, {}, &ok);
        if (!ok || url.trimmed().isEmpty()) return;
        load_asset_remote(url.trimmed(), true);
    }

    // Download a remote asset and its dependencies into .sim-cache/remote-downloads/<hash>/,
    // then load it via the standard path. Returns the root entity on success.
    std::optional<sim::EntityId> load_asset_remote(const QString& url, bool show_error) {
        if (!network_manager_)
            network_manager_ = new QNetworkAccessManager(this);

        const QString cache_subdir = [&] {
            QCryptographicHash hash(QCryptographicHash::Sha256);
            hash.addData(url.toUtf8());
            return hash.result().toHex().left(16);
        }();
        // .sim-cache is the ResourceManager's root; remote downloads live in a subdirectory.
        const QDir cache_dir = QDir(".sim-cache/remote-downloads/" + cache_subdir);

        auto download_sync = [&](const QString& target_url) -> QByteArray {
            QNetworkRequest request{QUrl(target_url)};
            request.setAttribute(QNetworkRequest::RedirectPolicyAttribute,
                                  QNetworkRequest::NoLessSafeRedirectPolicy);
            QNetworkReply* reply = network_manager_->get(request);
            QEventLoop loop;
            QObject::connect(reply, &QNetworkReply::finished, &loop, &QEventLoop::quit);
            loop.exec();
            QByteArray data;
            if (reply->error() == QNetworkReply::NoError)
                data = reply->readAll();
            else
                log(QString("Remote download failed (%1): %2").arg(reply->errorString(), target_url));
            reply->deleteLater();
            return data;
        };

        // Derive the entry filename from the URL path.
        const QString url_path = QUrl(url).path();
        const QString entry_name = url_path.contains('/')
            ? url_path.mid(url_path.lastIndexOf('/') + 1)
            : url_path;
        if (entry_name.isEmpty()) {
            if (show_error)
                QMessageBox::critical(this, "Remote asset error", "URL has no filename component");
            return std::nullopt;
        }

        // If the entry is already cached, load from cache.
        const QString entry_path = cache_dir.filePath(entry_name);
        if (!QFile::exists(entry_path)) {
            const QByteArray main_data = download_sync(url);
            if (main_data.isEmpty()) {
                if (show_error)
                    QMessageBox::critical(this, "Remote asset error",
                                          "Failed to download: " + url);
                return std::nullopt;
            }

            // Resolve dependencies with a simple text scan (no XML parser needed).
            QStringList dep_names;
            const QString suffix = QFileInfo(entry_name).suffix().toLower();
            const QString main_text = QString::fromUtf8(main_data);
            if (suffix == "obj") {
                for (const auto& line : main_text.split('\n')) {
                    const auto trimmed = line.trimmed();
                    if (trimmed.startsWith("mtllib ", Qt::CaseInsensitive)) {
                        const auto name = trimmed.mid(7).trimmed();
                        if (!name.isEmpty() && !dep_names.contains(name))
                            dep_names.append(name);
                    }
                }
            } else if (suffix == "urdf") {
                // Match filename="..." attributes on <mesh> and <texture> elements.
                static const QRegularExpression kFilename(
                    R"raw(<(?:mesh|texture)\b[^>]*\bfilename\s*=\s*"([^"]*)")raw",
                    QRegularExpression::CaseInsensitiveOption);
                auto it = kFilename.globalMatch(main_text);
                while (it.hasNext()) {
                    const auto match = it.next();
                    const auto name = match.captured(1).trimmed();
                    // Skip package:// and absolute paths — those can't be resolved as simple deps.
                    if (!name.isEmpty() && !name.startsWith("package://") &&
                            !QFileInfo(name).isAbsolute() && !dep_names.contains(name))
                        dep_names.append(name);
                }
            }

            // Build a map of relative path → bytes.
            std::unordered_map<QString, QByteArray> bundle;
            bundle[entry_name] = main_data;

            const QString base_url = url.left(url.lastIndexOf('/') + 1);
            for (const QString& dep : dep_names) {
                if (bundle.contains(dep)) continue;
                const QString dep_url = QUrl(base_url).resolved(QUrl(dep)).toString();
                const QByteArray dep_data = download_sync(dep_url);
                if (!dep_data.isEmpty())
                    bundle[dep] = dep_data;
                else
                    log(QString("Skipping missing remote dependency: %1").arg(dep));
            }

            // Write files to cache directory.
            cache_dir.mkpath(".");
            for (const auto& [rel, data] : bundle) {
                QFileInfo fi(cache_dir.filePath(rel));
                fi.dir().mkpath(".");
                QFile out(fi.filePath());
                if (!out.open(QIODevice::WriteOnly) || out.write(data) != data.size()) {
                    if (show_error)
                        QMessageBox::critical(this, "Remote asset error",
                                              "Failed to write cache file: " + rel);
                    return std::nullopt;
                }
            }
        }

        // Ensure a gateway-compatible bundle manifest exists so that when this asset
        // is referenced from a restored simstate the web gateway can recover the
        // remote_url and route the browser directly to the origin server.
        const QString manifest_path = cache_dir.filePath(".asset-bundle.json");
        if (!QFile::exists(manifest_path)) {
            QJsonObject manifest;
            manifest["entry"] = entry_name;
            manifest["remoteUrl"] = url;
            QFile mf(manifest_path);
            if (mf.open(QIODevice::WriteOnly | QIODevice::Text))
                mf.write(QJsonDocument(manifest).toJson(QJsonDocument::Compact));
        }

        return load_asset_path(entry_path, show_error);
    }

    std::optional<sim::EntityId> load_asset_path(const QString& path, bool show_error) {
        try {
            const auto result = resources_.load(path.toStdWString());
            const auto root = resources_.instantiate(*world_, result.asset);
            rebuild_active_backends();
            world_->capture_initial_state(); refresh_scene(); frame_scene();
            if (entity_items_.contains(root)) hierarchy_->setCurrentItem(entity_items_.at(root));
            log(QString("Loaded %1 (%2): %3 links, %4 visuals, %5 meshes, %6 textures")
                    .arg(path, result.from_cache ? "cache hit" : "imported")
                    .arg(result.asset.links.size()).arg(result.asset.visuals.size())
                    .arg(result.asset.meshes.size()).arg(result.asset.textures.size()));
            return root;
        } catch (const std::exception& error) {
            log(QString("Asset error for %1: %2").arg(path, QString::fromUtf8(error.what())));
            if (show_error)
                QMessageBox::critical(this, "Asset error", QString::fromUtf8(error.what()));
            return std::nullopt;
        }
    }

    void refresh_scene() {
        ifps_tree_cache_.clear();
        const auto previously_selected = selected_entity();
        std::unordered_map<sim::EntityId, bool> expansion_state;
        expansion_state.reserve(entity_items_.size());
        for (const auto& [entity, item] : entity_items_)
            expansion_state.emplace(entity, item->isExpanded());
        hierarchy_->clear();
        std::function<void(sim::EntityId, QTreeWidgetItem*)> append =
            [&](sim::EntityId entity, QTreeWidgetItem* parent) {
                const auto& name = world_->registry().get<sim::NameComponent>(entity).name;
                auto* item = parent ? new QTreeWidgetItem(parent) : new QTreeWidgetItem(hierarchy_);
                item->setText(0, QString("%1  [%2]").arg(QString::fromStdString(name)).arg(entity));
                item->setData(0, Qt::UserRole, QVariant::fromValue<qulonglong>(entity));
                entity_items_[entity] = item;
                const auto& children = world_->registry().get<sim::HierarchyComponent>(entity).children;
                for (const auto child : children)
                    if (world_->registry().valid(child)) append(child, item);
            };
        entity_items_.clear();
        for (const auto entity : world_->registry().entities())
            if (world_->registry().get<sim::HierarchyComponent>(entity).parent == sim::kInvalidEntity)
                append(entity, nullptr);
        hierarchy_->expandToDepth(2);
        for (const auto& [entity, expanded] : expansion_state)
            if (entity_items_.contains(entity)) entity_items_.at(entity)->setExpanded(expanded);
        if (previously_selected) reveal_entity(*previously_selected);
    }

    void reveal_entity(sim::EntityId entity) {
        if (!entity_items_.contains(entity)) return;
        auto* item = entity_items_.at(entity);
        for (auto* ancestor = item->parent(); ancestor != nullptr; ancestor = ancestor->parent())
            ancestor->setExpanded(true);
        hierarchy_->setCurrentItem(item);
        hierarchy_->scrollToItem(item, QAbstractItemView::PositionAtCenter);
    }

    [[nodiscard]] std::optional<sim::EntityId> selected_entity() const {
        if (!hierarchy_) return std::nullopt;
        const auto* item = hierarchy_->currentItem();
        if (!item) return std::nullopt;
        const auto entity = static_cast<sim::EntityId>(item->data(0, Qt::UserRole).toULongLong());
        if (!world_->registry().valid(entity)) return std::nullopt;
        return entity;
    }

    void save_state_file() {
        const QString path = QFileDialog::getSaveFileName(this, "Save Simulation State", {},
                                                          "Simulation State (*.simstate)");
        if (path.isEmpty()) return;
        try {
            sim::SaveStateFile(path.toStdWString(), {1, world_->snapshot(), {}});
            log(QString("Saved state: %1").arg(path));
        } catch (const std::exception& error) {
            QMessageBox::critical(this, "State error", QString::fromUtf8(error.what()));
        }
    }

    void restore_state_file() {
        const QString path = QFileDialog::getOpenFileName(this, "Restore Simulation State", {},
                                                          "Simulation State (*.simstate)");
        if (path.isEmpty()) return;
        try {
            world_->restore(sim::LoadStateFile(path.toStdWString()).world);
            rebuild_active_backends();
            refresh_scene(); render_dirty_ = true; render_viewport(); update_status();
            log(QString("Restored state: %1").arg(path));
        } catch (const std::exception& error) {
            QMessageBox::critical(this, "State error", QString::fromUtf8(error.what()));
        }
    }

    void command_joint_target() {
        const auto selected = selected_entity();
        if (!selected) return;
        const auto entity = *selected;
        if (!world_->registry().has<sim::RobotComponent>(entity)) {
            QMessageBox::information(this, "Joint Target", "Select a robot root entity first.");
            return;
        }
        sim::Robot robot(*world_, entity);
        bool accepted = false;
        const QString text = QInputDialog::getText(
            this, "Joint Position Target", "Comma-separated radians:", QLineEdit::Normal, {}, &accepted);
        if (!accepted) return;
        std::vector<double> targets;
        for (const QString& item : text.split(',', Qt::SkipEmptyParts)) {
            bool valid = false;
            const double value = item.trimmed().toDouble(&valid);
            if (!valid) {
                QMessageBox::warning(this, "Joint Target", "Every target must be a number.");
                return;
            }
            targets.push_back(value);
        }
        try {
            robot.command_joint_positions(targets);
            log(QString("Queued %1 joint targets for entity %2").arg(targets.size()).arg(entity));
        } catch (const std::exception& error) {
            QMessageBox::critical(this, "Joint Target", QString::fromUtf8(error.what()));
        }
    }

    void load_agent_log() {
        const QString path = QFileDialog::getOpenFileName(this, "Open Agent Command Log", {},
                                                          "JSON Lines (*.jsonl);;All Files (*)");
        if (path.isEmpty()) return;
        QFile file(path);
        if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
            QMessageBox::critical(this, "Agent Log", "Cannot open the selected log.");
            return;
        }
        agent_log_->setPlainText(QString::fromUtf8(file.readAll()));
    }

    void connect_agent_dialog() {
        bool accepted = false;
        const QString endpoint = QInputDialog::getText(
            this, "Connect Agent RPC", "Host:port", QLineEdit::Normal,
            "127.0.0.1:8765", &accepted);
        if (accepted) connect_agent_endpoint(endpoint);
    }

    void connect_agent_endpoint(const QString& endpoint) {
        const int separator = endpoint.lastIndexOf(':');
        bool valid_port = false;
        const quint16 port = endpoint.mid(separator + 1).toUShort(&valid_port);
        if (separator <= 0 || !valid_port || port == 0) {
            agent_log_->appendPlainText("Invalid Agent endpoint: " + endpoint);
            return;
        }
        agent_socket_->abort();
        agent_socket_->connectToHost(endpoint.left(separator), port);
    }

    void query_agent_scene() {
        if (agent_socket_->state() != QAbstractSocket::ConnectedState) return;
        const QJsonObject request{{"id", static_cast<qint64>(agent_request_id_++)},
                                  {"method", "scene.query"}, {"params", QJsonObject{}}};
        agent_socket_->write(QJsonDocument(request).toJson(QJsonDocument::Compact) + '\n');
    }

    void process_agent_responses() {
        agent_response_buffer_ += agent_socket_->readAll();
        int newline = -1;
        while ((newline = agent_response_buffer_.indexOf('\n')) >= 0) {
            const QByteArray line = agent_response_buffer_.left(newline);
            agent_response_buffer_.remove(0, newline + 1);
            QJsonParseError error;
            const auto document = QJsonDocument::fromJson(line, &error);
            if (error.error != QJsonParseError::NoError || !document.isObject()) continue;
            const auto response = document.object();
            if (!response.value("ok").toBool()) continue;
            const auto entities = response.value("result").toObject().value("entities").toArray();
            bool changed = false;
            for (const auto& item : entities) {
                const auto remote = item.toObject();
                const quint64 remote_id = static_cast<quint64>(remote.value("handle").toDouble());
                if (!agent_entities_.contains(remote_id)) {
                    agent_entities_[remote_id] = world_->create_entity("agent_" + std::to_string(remote_id));
                    changed = true;
                }
                const auto translation = remote.value("translation").toArray();
                if (translation.size() == 3) {
                    auto& transform = world_->registry().get<sim::TransformComponent>(agent_entities_.at(remote_id)).local;
                    transform.translation = {translation[0].toDouble(), translation[1].toDouble(),
                                             translation[2].toDouble()};
                }
            }
            world_->update_world_transforms();
            render_dirty_ = true;
            if (changed) refresh_scene();
            agent_log_->appendPlainText(QString::fromUtf8(line));
        }
    }

    void start_control_server() {
        control_server_ = new QTcpServer(this);
        connect(control_server_, &QTcpServer::newConnection, this, [this] {
            while (control_server_->hasPendingConnections()) {
                auto* socket = control_server_->nextPendingConnection();
                control_buffers_[socket] = {};
                control_workspaces_[socket] = "default";
                connect(socket, &QTcpSocket::readyRead, this,
                        [this, socket] { process_control_requests(socket); });
                connect(socket, &QTcpSocket::disconnected, this, [this, socket] {
                    control_buffers_.erase(socket);
                    control_workspaces_.erase(socket);
                    socket->deleteLater();
                });
            }
        });
        const QHostAddress address(control_options_.host);
        if (address.isNull() || !control_server_->listen(address, control_options_.port)) {
            log("Python SDK control server failed: " + control_server_->errorString());
            return;
        }
        if (control_options_.instance_id.isEmpty())
            control_options_.instance_id = QUuid::createUuid().toString(QUuid::WithoutBraces);
        const quint16 port = control_server_->serverPort();
        const bool wildcard = address == QHostAddress::Any || address == QHostAddress::AnyIPv4 ||
                              address == QHostAddress::AnyIPv6;
        const QString local_host = address.protocol() == QAbstractSocket::IPv6Protocol
            ? "[::1]" : "127.0.0.1";
        control_endpoints_.append(QString("tcp://%1:%2")
            .arg(wildcard ? local_host : control_options_.host).arg(port));
        for (const QString& source : control_options_.advertise) {
            QUrl endpoint(source);
            if (!endpoint.isValid() || endpoint.scheme() != "tcp" || endpoint.host().isEmpty())
                continue;
            if (endpoint.port() == 0) endpoint.setPort(port);
            control_endpoints_.append(endpoint.toString());
        }
        if (!control_options_.registry_directory.isEmpty()) {
            QDir().mkpath(control_options_.registry_directory);
            control_descriptor_path_ = QDir(control_options_.registry_directory).filePath(
                QString::number(QCoreApplication::applicationPid()) + ".json");
            const QJsonObject descriptor{
                {"protocol_version", 1},
                {"pid", static_cast<qint64>(QCoreApplication::applicationPid())},
                {"process_start_time", QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs)},
                {"instance_id", control_options_.instance_id},
                {"instance_name", control_options_.instance_name},
                {"endpoints", QJsonArray::fromStringList(control_endpoints_)}};
            QSaveFile output(control_descriptor_path_);
            if (output.open(QIODevice::WriteOnly)) {
                output.write(QJsonDocument(descriptor).toJson(QJsonDocument::Compact));
                if (!output.commit()) control_descriptor_path_.clear();
            } else {
                control_descriptor_path_.clear();
            }
        }
        log(QString("Python SDK control server listening on %1").arg(
            control_endpoints_.join(", ")));
    }

    void send_control_response(QTcpSocket* socket, const QJsonValue& id,
                               const QJsonObject& result) {
        const QJsonObject response{{"id", id}, {"ok", true}, {"result", result}};
        socket->write(QJsonDocument(response).toJson(QJsonDocument::Compact) + '\n');
    }

    void send_control_error(QTcpSocket* socket, const QJsonValue& id,
                            const QString& message) {
        const QJsonObject error{{"code", "simulation_error"}, {"message", message}};
        const QJsonObject response{{"id", id}, {"ok", false}, {"error", error}};
        socket->write(QJsonDocument(response).toJson(QJsonDocument::Compact) + '\n');
    }

    [[nodiscard]] QString control_kind(sim::EntityId entity) const {
        const auto& registry = world_->registry();
        if (registry.has<sim::RobotComponent>(entity)) return "robot";
        if (is_robot_link(entity)) return "link";
        return "entity";
    }

    [[nodiscard]] QJsonObject control_object_ref(sim::EntityId entity) const {
        if (!world_->registry().valid(entity)) throw std::runtime_error("object was not found");
        return {{"instance_id", control_options_.instance_id},
                {"world_id", static_cast<qint64>(world_->id())},
                {"object_id", QString::fromStdString(world_->object_id(entity))},
                {"runtime_handle", static_cast<qint64>(entity)},
                {"kind", control_kind(entity)},
                {"scene_generation", static_cast<qint64>(control_scene_generation_)}};
    }

    [[nodiscard]] sim::EntityId control_resolve_object(const QJsonValue& value) const {
        if (!value.isObject()) throw std::runtime_error("object reference is required");
        const auto reference = value.toObject();
        if (reference.value("instance_id").toString() != control_options_.instance_id)
            throw std::runtime_error("object belongs to another instance");
        if (reference.value("scene_generation").toVariant().toULongLong() !=
            control_scene_generation_)
            throw std::runtime_error("object reference is stale");
        const auto object_id = reference.value("object_id").toString().toStdString();
        const auto entity = world_->find_entity_by_object_id(object_id);
        if (entity == sim::kInvalidEntity) throw std::runtime_error("object was not found");
        return entity;
    }

    [[nodiscard]] std::string control_alias_key(QTcpSocket* socket,
                                                const QString& alias) const {
        const auto workspace = control_workspaces_.contains(socket)
            ? control_workspaces_.at(socket) : std::string("default");
        return workspace + "\n" + alias.toStdString();
    }

    void dispatch_control_request(QTcpSocket* socket, const QJsonObject& request) {
        const QJsonValue id = request.value("id");
        const QString method = request.value("method").toString();
        const QJsonObject params = request.value("params").toObject();
        try {
            if (method == "session.hello") {
                if (params.value("protocol_version").toInt() != 1)
                    throw std::runtime_error("protocol version is incompatible");
                const QString workspace = params.value("workspace").toString("default").trimmed();
                if (workspace.isEmpty()) throw std::runtime_error("workspace cannot be empty");
                control_workspaces_[socket] = workspace.toStdString();
                send_control_response(socket, id,
                    {{"protocol_version", 1},
                     {"pid", static_cast<qint64>(QCoreApplication::applicationPid())},
                     {"instance_id", control_options_.instance_id},
                     {"instance_name", control_options_.instance_name},
                     {"scene_revision", static_cast<qint64>(control_scene_revision_)},
                     {"scene_generation", static_cast<qint64>(control_scene_generation_)},
                     {"endpoints", QJsonArray::fromStringList(control_endpoints_)},
                     {"capabilities", QJsonArray{"scene.read", "scene.write",
                                                   "workspace.aliases", "robot.links",
                                                   "simulation.control"}}});
                return;
            }
            if (method == "session.ping") {
                send_control_response(socket, id,
                    {{"instance_id", control_options_.instance_id}});
                return;
            }
            if (method == "session.shutdown") {
                send_control_response(socket, id, {{"closing", true}});
                socket->flush();
                QTimer::singleShot(50, qApp, &QApplication::quit);
                return;
            }
            if (method == "workspace.resolve") {
                const QString alias = params.value("alias").toString().trimmed();
                const auto found = control_aliases_.find(control_alias_key(socket, alias));
                if (found == control_aliases_.end())
                    throw std::runtime_error("object alias was not found");
                if (params.contains("kind") &&
                    control_kind(found->second) != params.value("kind").toString())
                    throw std::runtime_error("object alias has a different type");
                send_control_response(socket, id,
                    {{"object", control_object_ref(found->second)}});
                return;
            }
            if (method == "workspace.get") {
                const auto workspace = control_workspaces_.contains(socket)
                    ? control_workspaces_.at(socket) : std::string("default");
                const std::string prefix = workspace + "\n";
                QJsonObject objects;
                for (const auto& [key, entity] : control_aliases_) {
                    if (!key.starts_with(prefix) || !world_->registry().valid(entity)) continue;
                    objects.insert(QString::fromStdString(key.substr(prefix.size())),
                                   control_object_ref(entity));
                }
                send_control_response(socket, id,
                    {{"revision", static_cast<qint64>(control_scene_revision_)},
                     {"objects", objects}});
                return;
            }
            if (method == "workspace.bind") {
                const QString alias = params.value("alias").toString().trimmed();
                if (alias.isEmpty()) throw std::runtime_error("alias cannot be empty");
                const auto entity = control_resolve_object(params.value("object"));
                const auto key = control_alias_key(socket, alias);
                const auto found = control_aliases_.find(key);
                if (found != control_aliases_.end() && found->second != entity)
                    throw std::runtime_error("object alias is already bound");
                control_aliases_[key] = entity;
                send_control_response(socket, id,
                    {{"alias", alias}, {"object", control_object_ref(entity)}});
                return;
            }
            if (method == "workspace.unbind") {
                const QString alias = params.value("alias").toString().trimmed();
                const bool removed = control_aliases_.erase(control_alias_key(socket, alias)) != 0;
                send_control_response(socket, id, {{"alias", alias}, {"removed", removed}});
                return;
            }
            if (method == "scene.ensure_robot") {
                const QString alias = params.value("alias").toString().trimmed();
                if (alias.isEmpty()) throw std::runtime_error("alias cannot be empty");
                const auto key = control_alias_key(socket, alias);
                const auto found = control_aliases_.find(key);
                if (found != control_aliases_.end()) {
                    if (!world_->registry().valid(found->second) ||
                        !world_->registry().has<sim::RobotComponent>(found->second))
                        throw std::runtime_error("object alias is stale or is not a robot");
                    const QByteArray requested_bytes = params.value("path").toString().toUtf8();
                    const auto requested_path = std::filesystem::absolute(PathFromUtf8(
                        std::string_view(requested_bytes.constData(),
                                         static_cast<std::size_t>(requested_bytes.size()))));
                    const auto existing_path = PathFromUtf8(
                        world_->registry().get<sim::RobotComponent>(found->second).model_resource);
                    std::error_code path_error;
                    if (!std::filesystem::equivalent(requested_path, existing_path, path_error) ||
                        path_error)
                        throw std::runtime_error("object alias is bound to a different asset");
                    send_control_response(socket, id,
                        {{"object", control_object_ref(found->second)}, {"created", false}});
                    return;
                }
                const auto root = load_asset_path(params.value("path").toString(), false);
                if (!root || !world_->registry().has<sim::RobotComponent>(*root))
                    throw std::runtime_error("asset is not a robot");
                control_aliases_[key] = *root;
                ++control_scene_revision_;
                send_control_response(socket, id,
                    {{"object", control_object_ref(*root)}, {"created", true}});
                return;
            }
            if (method == "scene.destroy_object") {
                const auto entity = control_resolve_object(params.value("object"));
                const std::string object_id = world_->object_id(entity);
                if (!world_->destroy_entity(entity))
                    throw std::runtime_error("object was not found");
                for (auto it = control_aliases_.begin(); it != control_aliases_.end();) {
                    if (it->second == entity) it = control_aliases_.erase(it);
                    else ++it;
                }
                ++control_scene_revision_;
                rebuild_active_backends();
                refresh_scene();
                render_dirty_ = true;
                send_control_response(socket, id,
                    {{"object_id", QString::fromStdString(object_id)}, {"destroyed", true}});
                return;
            }
            if (method == "scene.draw_trajectories") {
                // Batched on purpose: every candidate path of one motion arrives in a
                // single request, so the Qt tree is rebuilt once for the whole set rather
                // than once per path.
                QJsonArray drawn;
                std::size_t markers = 0;
                for (const auto& value : params.value("trajectories").toArray()) {
                    const auto entry = value.toObject();
                    const std::string name = entry.value("id").toString().toStdString();
                    if (name.empty()) throw std::runtime_error("trajectory id is required");
                    std::vector<sim::Vec3> points;
                    for (const auto& point : entry.value("points").toArray()) {
                        const auto coordinates = point.toArray();
                        if (coordinates.size() != 3)
                            throw std::runtime_error("each waypoint needs three coordinates");
                        points.push_back({coordinates[0].toDouble(), coordinates[1].toDouble(),
                                          coordinates[2].toDouble()});
                    }
                    std::array<float, 4> color{0.9F, 0.9F, 0.9F, 1.0F};
                    const auto requested = entry.value("color").toArray();
                    for (int channel = 0; channel < requested.size() && channel < 4; ++channel)
                        color[static_cast<std::size_t>(channel)] =
                            static_cast<float>(requested[channel].toDouble());
                    const std::size_t count = draw_trajectory_markers(
                        name, points, color, entry.value("radius").toDouble(0.008));
                    markers += count;
                    drawn.append(QJsonObject{{"id", QString::fromStdString(name)},
                                             {"markers", static_cast<qint64>(count)}});
                }
                ++control_scene_revision_;
                refresh_scene();
                render_dirty_ = true;
                send_control_response(socket, id,
                    {{"trajectories", drawn}, {"markers", static_cast<qint64>(markers)}});
                return;
            }
            if (method == "scene.clear_trajectories") {
                const std::size_t removed =
                    clear_trajectory_markers(params.value("id").toString().toStdString());
                if (removed > 0) {
                    ++control_scene_revision_;
                    refresh_scene();
                    render_dirty_ = true;
                }
                send_control_response(socket, id,
                    {{"cleared", static_cast<qint64>(removed)}});
                return;
            }
            if (method == "robot.set_joint_positions") {
                // Pose the robot outright, which is what a viewer needs: the editor does
                // not step physics, so a servo target set by command_joint_positions would
                // never be converged toward and the arm would not move at all.
                const auto robot_id = control_resolve_robot(params);
                sim::Robot robot(*world_, robot_id);
                std::vector<double> positions;
                for (const auto& value : params.value("positions").toArray())
                    positions.push_back(value.toDouble());
                robot.set_joint_positions(positions, sim::JointLimitPolicy::Clamp);
                world_->update_world_transforms();
                if (collision_) collision_->sync_from_scene(*world_);
                render_dirty_ = true;
                send_control_response(socket, id,
                    {{"robot", static_cast<qint64>(robot_id)},
                     {"joints", static_cast<qint64>(positions.size())}});
                return;
            }
            if (method == "scene.mirror_pose") {
                // The per-frame path, kept deliberately thin. It skips the backend rebuild
                // and the Qt tree rebuild that scene.set_transform performs, and does not
                // render inline: it marks the viewport dirty and returns, leaving the
                // frame timer to draw on its own 33 ms clock. A caller may therefore send
                // poses as fast as it likes without ever stalling the editor's UI thread.
                std::size_t joints = 0;
                const auto requested = params.value("joints").toArray();
                if (!requested.isEmpty()) {
                    sim::Robot robot(*world_, control_resolve_robot(params));
                    std::vector<double> positions;
                    for (const auto& value : requested) positions.push_back(value.toDouble());
                    // Clamp rather than Enforce: a mirrored pose comes from a solver whose
                    // waypoints may sit a hair outside a limit, and refusing one would
                    // abort the animation instead of merely flattening that joint.
                    robot.set_joint_positions(positions, sim::JointLimitPolicy::Clamp);
                    joints = positions.size();
                }
                std::size_t moved = 0;
                for (const auto& value : params.value("objects").toArray()) {
                    const auto entry = value.toObject();
                    const auto entity = entry.value("object").isObject()
                        ? control_resolve_object(entry.value("object"))
                        : static_cast<sim::EntityId>(entry.value("entity").toDouble());
                    if (!world_->registry().valid(entity)) continue;
                    auto& transform =
                        world_->registry().get<sim::TransformComponent>(entity).local;
                    const auto translation = entry.value("translation").toArray();
                    if (translation.size() == 3)
                        transform.translation = {translation[0].toDouble(),
                                                 translation[1].toDouble(),
                                                 translation[2].toDouble()};
                    const auto rotation = entry.value("rotation_wxyz").toArray();
                    if (rotation.size() == 4)
                        transform.rotation = {rotation[0].toDouble(), rotation[1].toDouble(),
                                             rotation[2].toDouble(), rotation[3].toDouble()};
                    ++moved;
                }
                world_->update_world_transforms();
                render_dirty_ = true;
                send_control_response(socket, id,
                    {{"joints", static_cast<qint64>(joints)},
                     {"objects", static_cast<qint64>(moved)}});
                return;
            }
            if (method == "robot.get_link") {
                const auto robot = control_resolve_object(params.value("robot"));
                const QString name = params.value("name").toString();
                const auto& registry = world_->registry();
                for (const auto link : registry.get<sim::RobotComponent>(robot).links) {
                    if (QString::fromStdString(registry.get<sim::NameComponent>(link).name) == name) {
                        send_control_response(socket, id,
                            {{"object", control_object_ref(link)}});
                        return;
                    }
                }
                throw std::runtime_error("robot link was not found");
            }
            if (method == "capabilities.query") {
                send_control_response(socket, id, {{"protocol_version", 1},
                    {"methods", QJsonArray{"scene.load_asset", "scene.load_robot",
                        "scene.load_asset_remote",
                        "scene.hierarchy", "scene.set_parent", "simulation.start",
                        "simulation.pause", "simulation.time", "simulation.status",
                        "robot.get_link",
                        "robot.joint_state",
                        "robot.command_joint_positions", "robot.command_joint_velocities",
                        "robot.set_joint_continuous", "robot.set_joint_positions",
                        "scene.draw_trajectories", "scene.clear_trajectories",
                        "scene.mirror_pose",
                        "scene.create_primitive", "scene.set_name", "scene.set_transform", "scene.get_transform",
                        "scene.set_rigid_body", "scene.create_camera", "scene.save_state",
                        "scene.load_state", "scene.set_backends", "sensor.set_enabled",
                        "collision.check",
                        "benchmark.create_collision_grid", "diagnostics.profile_step",
                        "diagnostics.performance", "application.quit"}}});
                return;
            }
            if (method == "scene.load_asset") {
                const auto root = load_asset_path(params.value("path").toString(), false);
                if (!root) throw std::runtime_error("asset could not be loaded");
                const auto& registry = world_->registry();
                send_control_response(socket, id,
                    {{"entity", static_cast<qint64>(*root)},
                     {"name", QString::fromStdString(
                         registry.get<sim::NameComponent>(*root).name)},
                     {"robot", registry.has<sim::RobotComponent>(*root)}});
                return;
            }
            if (method == "scene.load_asset_remote") {
                const auto url = params.value("url").toString();
                if (url.isEmpty()) throw std::runtime_error("url parameter is required");
                const auto root = load_asset_remote(url, false);
                if (!root) throw std::runtime_error("remote asset could not be downloaded or loaded");
                const auto& registry = world_->registry();
                send_control_response(socket, id,
                    {{"entity", static_cast<qint64>(*root)},
                     {"name", QString::fromStdString(
                         registry.get<sim::NameComponent>(*root).name)},
                     {"robot", registry.has<sim::RobotComponent>(*root)}});
                return;
            }
            if (method == "scene.hierarchy") {
                QJsonArray entities;
                const auto& registry = world_->registry();
                for (const auto entity : registry.entities()) {
                    const auto parent =
                        registry.get<sim::HierarchyComponent>(entity).parent;
                    QJsonObject item{
                        {"entity", static_cast<qint64>(entity)},
                        {"name", QString::fromStdString(
                            registry.get<sim::NameComponent>(entity).name)},
                        {"robot_root", registry.has<sim::RobotComponent>(entity)},
                        {"robot_link", is_robot_link(entity)},
                        {"camera_sensor", registry.has<sim::SensorComponent>(entity) &&
                                          registry.has<sim::CameraComponent>(entity)},
                        {"expanded", entity_items_.contains(entity) &&
                                     entity_items_.at(entity)->isExpanded()}};
                    item.insert("parent", parent == sim::kInvalidEntity
                        ? QJsonValue(QJsonValue::Null)
                        : QJsonValue(static_cast<qint64>(parent)));
                    entities.append(item);
                }
                send_control_response(socket, id, {{"entities", entities}});
                return;
            }
            if (method == "scene.set_parent") {
                const bool live_object = params.value("object").isObject();
                const auto entity = live_object
                    ? control_resolve_object(params.value("object"))
                    : static_cast<sim::EntityId>(params.value("entity").toDouble());
                const auto parent = params.value("parent").isNull() ||
                                    params.value("parent").isUndefined()
                    ? sim::kInvalidEntity
                    : (live_object ? control_resolve_object(params.value("parent"))
                                   : static_cast<sim::EntityId>(
                                       params.value("parent").toDouble()));
                const bool preserve_world = params.value("preserve_world").toBool(false);
                if (live_object) {
                    if (preserve_world) world_->set_parent_preserving_world(entity, parent);
                    else world_->set_parent(entity, parent);
                    refresh_scene();
                    render_dirty_ = true;
                    ++control_scene_revision_;
                } else if (!reparent_entity(entity, parent, false)) {
                    throw std::runtime_error(last_hierarchy_error_.toStdString());
                }
                QJsonObject result{{"entity", static_cast<qint64>(entity)}};
                result.insert("parent", parent == sim::kInvalidEntity
                    ? QJsonValue(QJsonValue::Null)
                    : QJsonValue(static_cast<qint64>(parent)));
                send_control_response(socket, id, result);
                return;
            }
            if (method == "scene.create_camera") {
                const QString output = params.value("output").toString("rgbd").toLower();
                if (output != "rgb" && output != "rgbd")
                    throw std::runtime_error("camera output must be rgb or rgbd");
                const auto parent = params.value("parent").isNull() ||
                                    params.value("parent").isUndefined()
                    ? sim::kInvalidEntity
                    : static_cast<sim::EntityId>(params.value("parent").toDouble());
                if (parent != sim::kInvalidEntity && !world_->registry().valid(parent))
                    throw std::runtime_error("camera parent was not found");
                const QString frame_id = params.value("frame_id").toString(
                    output == "rgbd" ? "rgbd_camera_optical_frame"
                                      : "rgb_camera_optical_frame");
                const double update_rate_hz = params.value("update_rate_hz").toDouble(30.0);
                const int width = params.value("width").toInt(640);
                const int height = params.value("height").toInt(480);
                const double vertical_fov_radians =
                    params.value("vertical_fov_radians").toDouble(1.0471975511965976);
                const double near_plane = params.value("near_plane").toDouble(0.1);
                const double far_plane = params.value("far_plane").toDouble(10.0);
                if (update_rate_hz <= 0.0 || width <= 0 || height <= 0 ||
                    vertical_fov_radians <= 0.0 ||
                    vertical_fov_radians >= 3.14159265358979323846 ||
                    near_plane <= 0.0 || far_plane <= near_plane)
                    throw std::runtime_error("camera parameters are invalid");
                const auto entity = create_camera_sensor(output == "rgbd"
                    ? sim::CameraOutput::RGBD : sim::CameraOutput::RGB, parent);
                auto& sensor = world_->registry().get<sim::SensorComponent>(entity);
                auto& camera = world_->registry().get<sim::CameraComponent>(entity);
                sensor.frame_id = frame_id.toStdString();
                sensor.update_rate_hz = update_rate_hz;
                camera.width = static_cast<std::uint32_t>(width);
                camera.height = static_cast<std::uint32_t>(height);
                camera.vertical_fov_radians = vertical_fov_radians;
                camera.near_plane = near_plane;
                camera.far_plane = far_plane;
                world_->update_world_transforms();
                world_->capture_initial_state();
                refresh_scene();
                reveal_entity(entity);
                send_control_response(socket, id, {{"entity", static_cast<qint64>(entity)}});
                return;
            }
            if (method == "scene.save_state") {
                const QString path = params.value("path").toString();
                if (path.isEmpty()) throw std::runtime_error("state path is required");
                sim::SaveStateFile(path.toStdWString(), {1, world_->snapshot(), {}});
                send_control_response(socket, id, {{"path", path}});
                return;
            }
            if (method == "scene.load_state") {
                const QString path = params.value("path").toString();
                if (path.isEmpty()) throw std::runtime_error("state path is required");
                world_->restore(sim::LoadStateFile(path.toStdWString()).world);
                ++control_scene_generation_;
                ++control_scene_revision_;
                control_aliases_.clear();
                rebuild_active_backends();
                refresh_scene();
                frame_scene();
                render_dirty_ = true;
                render_viewport();
                send_control_response(socket, id,
                    {{"path", path},
                     {"entities", static_cast<qint64>(world_->registry().size())}});
                return;
            }
            if (method == "scene.set_backends") {
                // Selects the editor's physics and collision backends, which otherwise
                // default to the combo boxes' first entry -- "None" for physics. A client
                // driving the editor as a display has no way to click those, so a scene
                // whose objects are meant to rest under gravity would sit unsimulated
                // while the client's own world simulated them, and the two would disagree.
                const auto pick = [&params](const char* key, const QString& fallback) {
                    const QString value = params.value(key).toString(fallback).toLower();
                    if (value == "none") return QString("None");
                    if (value == "mujoco") return QString("MuJoCo");
                    if (value == "bullet3") return QString("Bullet3");
                    if (value == "fcl") return QString("FCL");
                    if (value == "coal") return QString("Coal");
                    if (value == "ifps-cpu") return QString("IFPS-CPU");
                    if (value == "ifps-gpu") return QString("IFPS-GPU");
                    throw std::runtime_error("unsupported backend: " + value.toStdString());
                };
                const QString physics = pick("physics", physics_backend_->currentText());
                const QString collision = pick("collision", collision_backend_->currentText());
                for (const auto& [box, wanted] : {std::pair{physics_backend_, physics},
                                                  std::pair{collision_backend_, collision}})
                    if (box->findText(wanted) < 0)
                        throw std::runtime_error(
                            wanted.toStdString() + " is unavailable in this build");
                physics_backend_->setCurrentText(physics);
                collision_backend_->setCurrentText(collision);
                // Throws on a configuration the BackendManager rejects, which is why the
                // response reports what is actually active rather than what was asked for.
                apply_backend_configuration(true);
                render_dirty_ = true;
                send_control_response(socket, id,
                    {{"physics", physics_backend_->currentText()},
                     {"collision", collision_backend_->currentText()}});
                return;
            }
            if (method == "sensor.set_enabled") {
                const auto entity =
                    static_cast<sim::EntityId>(params.value("entity").toDouble());
                if (!world_->registry().valid(entity) ||
                    !world_->registry().has<sim::SensorComponent>(entity))
                    throw std::runtime_error("sensor entity was not found");
                const bool enabled = params.value("enabled").toBool(true);
                auto& sensor = world_->registry().get<sim::SensorComponent>(entity);
                sensor.next_update_time = enabled
                    ? world_->time().state().simulation_time
                    : std::numeric_limits<double>::infinity();
                send_control_response(socket, id,
                    {{"entity", static_cast<qint64>(entity)}, {"enabled", enabled}});
                return;
            }
            if (method == "benchmark.create_collision_grid") {
                if (!world_->registry().entities().empty())
                    throw std::runtime_error(
                        "collision-grid benchmark requires a newly launched empty editor");
                const int grid_size = params.value("grid_size").toInt(10);
                if (grid_size < 2)
                    throw std::runtime_error("collision-grid size must be at least 2");
                const double grid_offset = 0.5 * static_cast<double>(grid_size - 1);
                const QString requested_physics =
                    params.value("physics").toString("mujoco").toLower();
                if (requested_physics != "mujoco" && requested_physics != "bullet3")
                    throw std::runtime_error(
                        "collision-grid physics must be mujoco or bullet3");
#if !defined(SIM_HAS_BULLET_PLUGIN)
                if (requested_physics == "bullet3")
                    throw std::runtime_error("Bullet3 is unavailable in this build");
#endif
#if !defined(SIM_HAS_MUJOCO)
                if (requested_physics == "mujoco")
                    throw std::runtime_error("MuJoCo is unavailable in this build");
#endif
                const auto add_primitive = [&](const std::string& name,
                                               sim::ColliderShape shape,
                                               const std::string& mesh,
                                               const sim::Vec3& collider_size,
                                               const sim::Vec3& scale,
                                               const sim::Vec3& position,
                                               bool dynamic, double restitution) {
                    const auto entity = world_->create_entity(name);
                    auto& transform =
                        world_->registry().get<sim::TransformComponent>(entity).local;
                    transform.translation = position;
                    transform.scale = scale;
                    world_->registry().emplace<sim::VisualComponent>(entity,
                        sim::VisualComponent{mesh, {}, true, shape, {1.0, 1.0, 1.0},
                                             {0.62F, 0.68F, 0.76F, 1.0F}});
                    world_->registry().emplace<sim::ColliderComponent>(entity,
                        sim::ColliderComponent{shape, collider_size, {}, false,
                                               {1.0, 0.005, 0.0001}, restitution});
                    world_->registry().emplace<sim::RigidBodyComponent>(entity,
                        sim::RigidBodyComponent{1.0, {}, {}, dynamic, dynamic});
                    return entity;
                };
                const auto plane = add_primitive(
                    "Benchmark Floor Cube", sim::ColliderShape::Box, {},
                    {1.0, 1.0, 1.0},
                    {static_cast<double>(grid_size), static_cast<double>(grid_size), 10.0},
                    {0.0, 0.0, -4.99}, false, 1.0);
                QJsonArray balls;
                for (int i = 0; i < grid_size; ++i) {
                    const double height = 2.0 + 8.0 * static_cast<double>(i) /
                        static_cast<double>(grid_size - 1);
                    for (int j = 0; j < grid_size; ++j) {
                        const double restitution = static_cast<double>(j) /
                            static_cast<double>(grid_size - 1);
                        const auto ball = add_primitive(
                            "Benchmark Ball " + std::to_string(i) + "_" + std::to_string(j),
                            sim::ColliderShape::Sphere, "builtin://sphere",
                            {0.5, 0.5, 0.5}, {0.5, 0.5, 0.5},
                            {-grid_offset + static_cast<double>(i),
                             -grid_offset + static_cast<double>(j),
                             height}, true, restitution);
                        balls.append(static_cast<qint64>(ball));
                    }
                }
                world_->update_world_transforms();
                world_->capture_initial_state();
                physics_backend_->setCurrentText(
                    requested_physics == "bullet3" ? "Bullet3" : "MuJoCo");
                const QString requested_collision = params.value("collision").toString("fcl").toLower();
                if (requested_collision != "fcl" && requested_collision != "bullet3" &&
                    requested_collision != "coal" &&
                    requested_collision != "ifps-cpu" &&
                    requested_collision != "ifps-gpu")
                    throw std::runtime_error(
                        "benchmark collision must be fcl, bullet3, coal, ifps-cpu, or ifps-gpu");
                collision_backend_->setCurrentText(requested_collision == "bullet3" ? "Bullet3"
                    : requested_collision == "coal" ? "Coal"
                    : requested_collision == "ifps-cpu" ? "IFPS-CPU"
                    : requested_collision == "ifps-gpu" ? "IFPS-GPU" : "FCL");
                render_backend_->setCurrentText("Vulkan");
                apply_backend_configuration(false);
                const sim::BackendKind expected_physics = requested_physics == "bullet3"
                    ? sim::BackendKind::Bullet3 : sim::BackendKind::MuJoCo;
                if (backend_configuration_.physics != expected_physics)
                    throw std::runtime_error("failed to activate the requested physics backend");
                refresh_scene();
                camera_target_ = {0.0, 0.0, 6.0};
                camera_distance_ = std::max(22.0, 1.8 * static_cast<double>(grid_size));
                render_dirty_ = true;
                render_viewport();
                send_control_response(socket, id,
                    {{"plane", static_cast<qint64>(plane)}, {"balls", balls},
                     {"grid_size", grid_size},
                     {"physics", physics_backend_->currentText()},
                     {"collision", collision_backend_->currentText()},
                     {"renderer", "Vulkan"}});
                return;
            }
            if (method == "diagnostics.profile_step") {
                const auto requested = params.value("entities").toArray();
                std::vector<sim::EntityId> entities;
                entities.reserve(static_cast<std::size_t>(requested.size()));
                for (const auto& value : requested) {
                    const auto entity = static_cast<sim::EntityId>(value.toDouble());
                    if (!world_->registry().valid(entity))
                        throw std::runtime_error("profile entity was not found");
                    entities.push_back(entity);
                }
                const StepTimings step_timings = step_simulation(1, false);
                QElapsedTimer timer;
                QJsonArray collisions;
                timer.start();
                if (!collision_)
                    throw std::runtime_error("collision backend is not active");
                const auto collision_result = collision_->check_scene_collisions(entities);
                const double collision_ms = static_cast<double>(timer.nsecsElapsed()) / 1.0e6;
                for (const auto colliding : collision_result)
                    collisions.append(colliding != 0U);
                double render_ms = 0.0;
                if (params.value("render").toBool(true)) {
                    timer.restart();
                    render_viewport();
                    render_ms = static_cast<double>(timer.nsecsElapsed()) / 1.0e6;
                }
                send_control_response(socket, id,
                    {{"simulation_time", world_->time().state().simulation_time},
                     {"physics_tick_ms", step_timings.physics_ms},
                     {"collision_sync_ms", step_timings.collision_sync_ms},
                     {"collision_check_ms", collision_ms},
                     {"render_ms", render_ms}, {"collisions", collisions}});
                return;
            }
            if (method == "scene.set_transform") {
                const auto entity = params.value("object").isObject()
                    ? control_resolve_object(params.value("object"))
                    : static_cast<sim::EntityId>(params.value("entity").toDouble());
                if (!world_->registry().valid(entity))
                    throw std::runtime_error("entity was not found");
                auto& transform = world_->registry().get<sim::TransformComponent>(entity).local;
                const auto read_vec3 = [&](const char* name, sim::Vec3 fallback) {
                    const auto values = params.value(name).toArray();
                    if (values.isEmpty()) return fallback;
                    if (values.size() != 3)
                        throw std::runtime_error(std::string(name) + " must contain three values");
                    return sim::Vec3{values[0].toDouble(), values[1].toDouble(),
                                     values[2].toDouble()};
                };
                transform.translation = read_vec3("translation", transform.translation);
                transform.scale = read_vec3("scale", transform.scale);
                const auto rotation = params.value("rotation_wxyz").toArray();
                if (!rotation.isEmpty()) {
                    if (rotation.size() != 4)
                        throw std::runtime_error("rotation_wxyz must contain four values");
                    transform.rotation = {rotation[0].toDouble(), rotation[1].toDouble(),
                                          rotation[2].toDouble(), rotation[3].toDouble()};
                }
                world_->update_world_transforms();
                rebuild_active_backends();
                refresh_scene();
                render_dirty_ = true;
                ++control_scene_revision_;
                send_control_response(socket, id,
                    {{"entity", static_cast<qint64>(entity)},
                     {"object", control_object_ref(entity)}});
                return;
            }
            if (method == "scene.set_name") {
                const auto entity = params.value("object").isObject()
                    ? control_resolve_object(params.value("object"))
                    : static_cast<sim::EntityId>(params.value("entity").toDouble());
                if (!world_->registry().valid(entity))
                    throw std::runtime_error("entity was not found");
                const auto name = params.value("name").toString().trimmed();
                if (name.isEmpty()) throw std::runtime_error("entity name is required");
                world_->registry().get<sim::NameComponent>(entity).name = name.toStdString();
                world_->capture_initial_state();
                refresh_scene();
                render_dirty_ = true;
                ++control_scene_revision_;
                send_control_response(socket, id,
                    {{"entity", static_cast<qint64>(entity)},
                     {"name", name}, {"object", control_object_ref(entity)}});
                return;
            }
            if (method == "scene.update_transform") {
                const auto updated = request;
                QJsonObject forwarded = updated;
                forwarded.insert("method", "scene.set_transform");
                dispatch_control_request(socket, forwarded);
                return;
            }
            if (method == "scene.get_transform") {
                const auto entity = params.value("object").isObject()
                    ? control_resolve_object(params.value("object"))
                    : static_cast<sim::EntityId>(params.value("entity").toDouble());
                if (!world_->registry().valid(entity))
                    throw std::runtime_error("entity was not found");
                const auto& transform =
                    world_->registry().get<sim::TransformComponent>(entity).local;
                send_control_response(socket, id,
                    {{"translation", QJsonArray{transform.translation.x,
                                                  transform.translation.y,
                                                  transform.translation.z}},
                     {"rotation_wxyz", QJsonArray{transform.rotation.w, transform.rotation.x,
                                                   transform.rotation.y, transform.rotation.z}},
                     {"scale", QJsonArray{transform.scale.x, transform.scale.y,
                                           transform.scale.z}}});
                return;
            }
            if (method == "scene.set_rigid_body") {
                const auto entity = static_cast<sim::EntityId>(params.value("entity").toDouble());
                if (!world_->registry().valid(entity))
                    throw std::runtime_error("entity was not found");
                if (robot_owner(entity))
                    throw std::runtime_error(
                        "robot rigid bodies are managed by articulated joints and cannot be set Dynamic");
                if (entity_items_.contains(entity))
                    hierarchy_->setCurrentItem(entity_items_.at(entity));
                inspector_updating_ = true;
                body_enabled_edit_->setChecked(params.value("enabled").toBool(true));
                body_type_edit_->setCurrentIndex(params.value("dynamic").toBool(false) ? 1 : 0);
                gravity_edit_->setChecked(params.value("gravity").toBool(true));
                mass_edit_->setValue(params.value("mass").toDouble(1.0));
                if (params.contains("restitution"))
                    restitution_edit_->setValue(params.value("restitution").toDouble());
                if (params.contains("friction")) {
                    const auto friction = params.value("friction").toArray();
                    if (friction.size() != 3)
                        throw std::runtime_error("friction must contain three values");
                    for (int index = 0; index < 3; ++index)
                        friction_edit_[index]->setValue(friction[index].toDouble());
                }
                inspector_updating_ = false;
                apply_rigid_body_inspector();
                send_control_response(socket, id,
                    {{"entity", static_cast<qint64>(entity)},
                     {"physics", physics_backend_->currentText()}});
                return;
            }
            if (method == "scene.create_primitive") {
                const QString kind = params.value("kind").toString().toLower();
                sim::EntityId entity = sim::kInvalidEntity;
                QString label;
                if (kind == "cube") {
                    label = "Cube 1m";
                    entity = create_primitive(label, sim::ColliderShape::Box, {},
                                              {1.0, 1.0, 1.0});
                } else if (kind == "sphere") {
                    label = "Sphere 1m";
                    entity = create_primitive(label, sim::ColliderShape::Sphere,
                                              "builtin://sphere", {0.5, 0.5, 0.5});
                } else if (kind == "plane") {
                    label = "Plane 1m";
                    entity = create_primitive(label, sim::ColliderShape::Box,
                                              "builtin://plane", {1.0, 1.0, 0.02});
                } else if (kind == "capsule") {
                    label = "Capsule 1m";
                    entity = create_primitive(label, sim::ColliderShape::Capsule,
                                              "builtin://capsule", {0.25, 0.25, 0.5});
                } else if (kind == "cylinder") {
                    label = "Cylinder 1m";
                    entity = create_primitive(label, sim::ColliderShape::Cylinder,
                                              "builtin://cylinder", {0.5, 0.5, 1.0});
                } else {
                    throw std::runtime_error(
                        "primitive kind must be cube, sphere, plane, capsule, or cylinder");
                }
                ++control_scene_revision_;
                send_control_response(socket, id,
                    {{"entity", static_cast<qint64>(entity)}, {"name", label},
                     {"visual", true}, {"collider", true},
                     {"object", control_object_ref(entity)}});
                return;
            }
            if (method == "collision.check") {
                if (!collision_)
                    throw std::runtime_error("collision backend is not active");
                const auto first = static_cast<sim::EntityId>(params.value("first").toDouble());
                const auto second = static_cast<sim::EntityId>(params.value("second").toDouble());
                send_control_response(socket, id,
                    {{"colliding", collision_->check_collision(first, second)}});
                return;
            }
            if (method == "scene.load_robot") {
                const auto root = load_asset_path(params.value("path").toString(), false);
                if (!root || !world_->registry().has<sim::RobotComponent>(*root))
                    throw std::runtime_error("asset is not a robot");
                sim::Robot robot(*world_, *root);
                QJsonArray names;
                for (const auto& name : robot.joint_state().names)
                    names.append(QString::fromStdString(name));
                if (params.contains("alias")) {
                    const QString alias = params.value("alias").toString().trimmed();
                    if (alias.isEmpty()) throw std::runtime_error("alias cannot be empty");
                    const auto key = control_alias_key(socket, alias);
                    const auto found = control_aliases_.find(key);
                    if (found != control_aliases_.end() && found->second != *root)
                        throw std::runtime_error("object alias is already bound");
                    control_aliases_[key] = *root;
                }
                ++control_scene_revision_;
                send_control_response(socket, id,
                    {{"robot", static_cast<qint64>(*root)},
                     {"joint_count", static_cast<qint64>(robot.joints().size())},
                     {"joint_names", names},
                     {"object", control_object_ref(*root)}});
                return;
            }
            if (method == "robot.command_joint_velocities") {
                const auto robot_id = static_cast<sim::EntityId>(params.value("robot").toDouble());
                sim::Robot robot(*world_, robot_id);
                std::vector<double> targets;
                for (const auto& value : params.value("targets").toArray())
                    targets.push_back(value.toDouble());
                robot.command_joint_velocities(targets);
                world_->capture_initial_state();
                log(QString("Python SDK velocity command for robot %1").arg(robot_id));
                send_control_response(socket, id, {{"robot", static_cast<qint64>(robot_id)}});
                return;
            }
            if (method == "robot.command_joint_positions") {
                const auto robot_id = params.value("robot_object").isObject()
                    ? control_resolve_object(params.value("robot_object"))
                    : static_cast<sim::EntityId>(params.value("robot").toDouble());
                sim::Robot robot(*world_, robot_id);
                std::vector<double> targets;
                for (const auto& value : params.value("targets").toArray())
                    targets.push_back(value.toDouble());
                std::vector<double> effort_limits;
                if (params.contains("effort_limits") && !params.value("effort_limits").isNull()) {
                    const auto limits = params.value("effort_limits");
                    if (!limits.isArray() || limits.toArray().size() != targets.size())
                        throw sim::Error(sim::ErrorCode::InvalidArgument,
                                         "joint effort limit count does not match joint targets");
                    for (const auto& value : limits.toArray()) {
                        if (value.isNull()) effort_limits.push_back(std::numeric_limits<double>::infinity());
                        else if (value.isDouble() && std::isfinite(value.toDouble()) && value.toDouble() >= 0.0)
                            effort_limits.push_back(value.toDouble());
                        else throw sim::Error(sim::ErrorCode::InvalidArgument,
                                              "effort limits must be finite nonnegative numbers or null");
                    }
                }
                robot.command_joint_positions(
                    targets, params.value("gain").toDouble(10.0),
                    params.value("velocity_limit").toDouble(1.0), effort_limits);
                world_->capture_initial_state();
                log(QString("Python SDK position command for robot %1").arg(robot_id));
                send_control_response(socket, id, {{"robot", static_cast<qint64>(robot_id)}});
                return;
            }
            if (method == "robot.set_joint_continuous") {
                const auto robot_id = params.value("robot_object").isObject()
                    ? control_resolve_object(params.value("robot_object"))
                    : static_cast<sim::EntityId>(params.value("robot").toDouble());
                sim::Robot robot(*world_, robot_id);
                const std::string requested = params.value("joint").toString().toStdString();
                const bool enabled = params.value("enabled").toBool(true);
                robot.set_joint_continuous(requested, enabled);
                log(QString("Python SDK set %1 continuous=%2")
                        .arg(QString::fromStdString(requested)).arg(enabled));
                send_control_response(socket, id, {{"joint", QString::fromStdString(requested)},
                                                    {"continuous", enabled}});
                return;
            }
            if (method == "robot.joint_state") {
                const auto robot_id = params.value("robot_object").isObject()
                    ? control_resolve_object(params.value("robot_object"))
                    : static_cast<sim::EntityId>(params.value("robot").toDouble());
                const auto state = sim::Robot(*world_, robot_id).joint_state();
                QJsonArray names, positions, velocities, efforts;
                for (const auto& value : state.names)
                    names.append(QString::fromStdString(value));
                for (double value : state.positions) positions.append(value);
                for (double value : state.velocities) velocities.append(value);
                for (double value : state.efforts) efforts.append(value);
                send_control_response(socket, id, {{"names", names}, {"positions", positions},
                    {"velocities", velocities}, {"efforts", efforts}});
                return;
            }
            if (method == "simulation.start") {
                world_->start();
                log("Simulation started by Python SDK");
                send_control_response(socket, id, {{"status", "running"}});
                return;
            }
            if (method == "simulation.pause") {
                world_->pause();
                log("Simulation paused by Python SDK");
                send_control_response(socket, id, {{"status", "paused"}});
                return;
            }
            if (method == "simulation.time") {
                send_control_response(socket, id,
                    {{"time", world_->time().state().simulation_time}});
                return;
            }
            if (method == "simulation.status") {
                const char* status = "stopped";
                if (world_->status() == sim::SimulationStatus::Running) status = "running";
                else if (world_->status() == sim::SimulationStatus::Paused) status = "paused";
                send_control_response(socket, id,
                    {{"status", status},
                     {"time", world_->time().state().simulation_time}});
                return;
            }
            if (method == "diagnostics.performance") {
                send_control_response(socket, id, {{"render_fps", render_fps_},
                    {"render_ms", average_render_ms_}});
                return;
            }
            if (method == "application.quit") {
                send_control_response(socket, id, {{"closing", true}});
                QTimer::singleShot(0, qApp, &QApplication::quit);
                return;
            }
            throw std::runtime_error("unknown Python SDK method: " + method.toStdString());
        } catch (const std::exception& error) {
            send_control_error(socket, id, QString::fromUtf8(error.what()));
        }
    }

    void process_control_requests(QTcpSocket* socket) {
        auto& buffer = control_buffers_.at(socket);
        buffer += socket->readAll();
        int newline = -1;
        while ((newline = buffer.indexOf('\n')) >= 0) {
            const QByteArray line = buffer.left(newline);
            buffer.remove(0, newline + 1);
            QJsonParseError error;
            const auto document = QJsonDocument::fromJson(line, &error);
            if (error.error != QJsonParseError::NoError || !document.isObject()) {
                send_control_error(socket, {}, "invalid JSON request");
                continue;
            }
            dispatch_control_request(socket, document.object());
        }
    }

    [[nodiscard]] std::optional<sim::EntityId> robot_owner(sim::EntityId entity) const {
        const auto& registry = world_->registry();
        while (entity != sim::kInvalidEntity && registry.valid(entity)) {
            if (registry.has<sim::RobotComponent>(entity)) return entity;
            entity = registry.get<sim::HierarchyComponent>(entity).parent;
        }
        return std::nullopt;
    }

    [[nodiscard]] bool is_robot_link(sim::EntityId entity) const {
        const auto& registry = world_->registry();
        if (registry.has<sim::JointComponent>(entity)) return true;
        for (const auto robot : registry.view<sim::RobotComponent>()) {
            const auto& links = registry.get<sim::RobotComponent>(robot).links;
            if (std::find(links.begin(), links.end(), entity) != links.end()) return true;
        }
        return false;
    }

    bool reparent_entity(sim::EntityId child, sim::EntityId parent, bool show_error) {
        try {
            auto& registry = world_->registry();
            if (!registry.valid(child) ||
                (parent != sim::kInvalidEntity && !registry.valid(parent)))
                throw std::runtime_error("child or parent entity was not found");
            if (is_robot_link(child))
                throw std::runtime_error(
                    "robot links are managed by articulated joints; move the robot root instead");
            const auto& child_name = registry.get<sim::NameComponent>(child).name;
            const QString parent_name = parent == sim::kInvalidEntity
                ? "World root"
                : QString::fromStdString(registry.get<sim::NameComponent>(parent).name);
            world_->set_parent_preserving_world(child, parent);
            rebuild_active_backends();
            world_->capture_initial_state();
            refresh_scene();
            reveal_entity(child);
            render_dirty_ = true;
            render_viewport();
            log(QString("Reparented %1 [%2] under %3")
                    .arg(QString::fromStdString(child_name)).arg(child).arg(parent_name));
            last_hierarchy_error_.clear();
            return true;
        } catch (const std::exception& error) {
            refresh_scene();
            const QString message = QString::fromUtf8(error.what());
            last_hierarchy_error_ = message;
            log("Hierarchy change rejected: " + message);
            if (show_error) QMessageBox::warning(this, "Invalid hierarchy", message);
            return false;
        }
    }

    [[nodiscard]] static std::vector<std::uint32_t> ifps_level_counts(
        const sim::ColliderComponent& collider) {
        if (collider.shape == sim::ColliderShape::Sphere) return {1};
        const std::uint32_t leaf_count = collider.ifps_sphere_count == 0
            ? 256U : collider.ifps_sphere_count;
        std::vector<std::uint32_t> counts{1};
        while (counts.back() < leaf_count) {
            const std::uint32_t next = counts.back() > leaf_count / 8U
                ? leaf_count : counts.back() * 8U;
            counts.push_back(next);
        }
        return counts;
    }

    void save_ifps_level_mask() {
        const auto selected = selected_entity();
        if (!selected) return;
        std::uint32_t mask = 0;
        for (std::size_t index = 0; index < ifps_level_edits_.size(); ++index)
            if (ifps_level_edits_[index]->isVisible() &&
                ifps_level_edits_[index]->isChecked())
                mask |= 1U << index;
        ifps_level_masks_[*selected] = mask;
    }

    void update_ifps_level_controls(sim::EntityId entity) {
        const auto& collider = world_->registry().get<sim::ColliderComponent>(entity);
        const auto counts = ifps_level_counts(collider);
        const auto found = ifps_level_masks_.find(entity);
        const std::uint32_t default_mask = 1U << (counts.size() - 1U);
        const std::uint32_t mask = found == ifps_level_masks_.end()
            ? default_mask : found->second;
        for (std::size_t index = 0; index < ifps_level_edits_.size(); ++index) {
            const bool available = index < counts.size();
            ifps_level_edits_[index]->setVisible(available);
            ifps_level_edits_[index]->setEnabled(available);
            if (available) {
                ifps_level_edits_[index]->setText(
                    QString("Level %1 (%2 spheres)").arg(index).arg(counts[index]));
                ifps_level_edits_[index]->setChecked((mask & (1U << index)) != 0U);
            } else {
                ifps_level_edits_[index]->setChecked(false);
            }
        }
        if ((mask & ((1U << counts.size()) - 1U)) == 0U) {
            ifps_level_edits_[counts.size() - 1U]->setChecked(true);
            ifps_level_masks_[entity] = default_mask;
        }
    }

    void apply_ifps_inspector() {
        if (inspector_updating_) return;
        const auto selected = selected_entity();
        if (!selected || !world_->registry().has<sim::ColliderComponent>(*selected)) return;
        auto& collider = world_->registry().get<sim::ColliderComponent>(*selected);
        collider.ifps_sphere_count = static_cast<std::uint32_t>(ifps_count_edit_->value());
        ifps_tree_cache_.erase(*selected);
        ifps_level_masks_.erase(*selected);
        inspector_updating_ = true;
        update_ifps_level_controls(*selected);
        inspector_updating_ = false;
        rebuild_active_backends();
        world_->capture_initial_state();
        render_dirty_ = true;
        render_viewport();
    }

    void inspect(QTreeWidgetItem* item) {
        inspector_updating_ = true;
        const auto selected = selected_entity();
        inspector_widget_->setEnabled(selected.has_value());
        physics_widget_->setEnabled(selected.has_value());
        ifps_widget_->setEnabled(selected.has_value() &&
                                 world_->registry().has<sim::ColliderComponent>(*selected));
        camera_widget_->setEnabled(selected.has_value() &&
                                   world_->registry().has<sim::CameraComponent>(*selected));
        if (!selected) {
            light_widget_->setVisible(false);
            components_label_->setText("Select an entity to inspect and edit it.");
            inspector_updating_ = false;
            return;
        }
        const auto entity = *selected;
        const auto& registry = world_->registry();
        const auto& name = registry.get<sim::NameComponent>(entity).name;
        const auto& transform = registry.get<sim::TransformComponent>(entity).local;
        const auto euler = QuaternionEulerDegrees(transform.rotation);
        name_edit_->setText(QString::fromStdString(name));
        const std::array<double, 3> position{transform.translation.x, transform.translation.y,
                                             transform.translation.z};
        const std::array<double, 3> rotation{euler.x, euler.y, euler.z};
        const std::array<double, 3> scale{transform.scale.x, transform.scale.y, transform.scale.z};
        for (int index = 0; index < 3; ++index) {
            position_edit_[index]->setValue(position[static_cast<std::size_t>(index)]);
            rotation_edit_[index]->setValue(rotation[static_cast<std::size_t>(index)]);
            scale_edit_[index]->setValue(scale[static_cast<std::size_t>(index)]);
        }
        visible_edit_->setEnabled(registry.has<sim::VisualComponent>(entity));
        visible_edit_->setChecked(!registry.has<sim::VisualComponent>(entity) ||
                                  registry.get<sim::VisualComponent>(entity).visible);
        const bool has_visual = registry.has<sim::VisualComponent>(entity);
        min_lod_edit_->setEnabled(has_visual);
        max_lod_edit_->setEnabled(has_visual);
        if (has_visual) {
            const auto& visual = registry.get<sim::VisualComponent>(entity);
            min_lod_edit_->setValue(visual.min_lod);
            max_lod_edit_->setValue(visual.max_lod);
        } else {
            min_lod_edit_->setValue(0);
            max_lod_edit_->setValue(7);
        }
        const bool has_light = registry.has<sim::LightComponent>(entity);
        light_widget_->setVisible(has_light);
        light_type_edit_->setEnabled(has_light); light_enabled_edit_->setEnabled(has_light);
        light_color_edit_->setEnabled(has_light); light_intensity_edit_->setEnabled(has_light);
        if (has_light) {
            const auto& light = registry.get<sim::LightComponent>(entity);
            light_type_edit_->setCurrentIndex(static_cast<int>(light.type));
            light_enabled_edit_->setChecked(light.enabled);
            light_intensity_edit_->setValue(light.intensity);
            light_color_ = QColor::fromRgbF(std::clamp(light.color[0], 0.0F, 1.0F),
                                            std::clamp(light.color[1], 0.0F, 1.0F),
                                            std::clamp(light.color[2], 0.0F, 1.0F));
            light_color_edit_->setText(light_color_.name(QColor::HexRgb).toUpper());
            light_color_edit_->setStyleSheet(QString("background-color:%1;color:%2")
                .arg(light_color_.name(), light_color_.lightnessF() < 0.5 ? "white" : "black"));
            light_range_edit_->setValue(light.range);
            light_decay_edit_->setValue(light.decay);
            constexpr double degrees = 180.0 / 3.14159265358979323846;
            light_inner_cone_edit_->setValue(light.inner_cone_radians * degrees);
            light_outer_cone_edit_->setValue(light.outer_cone_radians * degrees);
            light_angular_diameter_edit_->setValue(light.angular_diameter_radians * degrees);
        }
        const bool positional_light = has_light &&
            light_type_edit_->currentIndex() != static_cast<int>(sim::LightType::Directional) &&
            light_type_edit_->currentIndex() != static_cast<int>(sim::LightType::Ambient);
        const bool spot_light = has_light &&
            light_type_edit_->currentIndex() == static_cast<int>(sim::LightType::Spot);
        const bool directional_light = has_light &&
            light_type_edit_->currentIndex() == static_cast<int>(sim::LightType::Directional);
        light_range_edit_->setEnabled(positional_light);
        light_decay_edit_->setEnabled(positional_light);
        light_inner_cone_edit_->setEnabled(spot_light);
        light_outer_cone_edit_->setEnabled(spot_light);
        light_angular_diameter_edit_->setEnabled(directional_light);
        const bool has_body = registry.has<sim::RigidBodyComponent>(entity);
        const bool robot_managed = robot_owner(entity).has_value();
        body_enabled_edit_->setChecked(has_body);
        body_enabled_edit_->setEnabled(!robot_managed);
        body_type_edit_->setEnabled(has_body && !robot_managed);
        gravity_edit_->setEnabled(has_body && !robot_managed &&
                                  registry.get<sim::RigidBodyComponent>(entity).dynamic);
        mass_edit_->setEnabled(has_body && !robot_managed);
        const QString body_help = robot_managed
            ? "Robot rigid bodies are managed by the articulated-joint system. The base is fixed; joint links are powered and gravity-compensated."
            : "Enable a static or dynamic rigid body for this scene entity.";
        body_enabled_edit_->setToolTip(body_help);
        body_type_edit_->setToolTip(body_help);
        if (has_body) {
            const auto& body = registry.get<sim::RigidBodyComponent>(entity);
            body_type_edit_->setCurrentIndex(body.dynamic ? 1 : 0);
            gravity_edit_->setChecked(body.gravity_enabled);
            mass_edit_->setValue(body.mass);
        } else {
            body_type_edit_->setCurrentIndex(0);
            gravity_edit_->setChecked(false);
            mass_edit_->setValue(1.0);
        }
        const bool has_collider = registry.has<sim::ColliderComponent>(entity);
        for (auto* field : friction_edit_) field->setEnabled(has_collider);
        restitution_edit_->setEnabled(has_collider);
        if (has_collider) {
            const auto& collider = registry.get<sim::ColliderComponent>(entity);
            friction_edit_[0]->setValue(collider.friction.x);
            friction_edit_[1]->setValue(collider.friction.y);
            friction_edit_[2]->setValue(collider.friction.z);
            restitution_edit_->setValue(collider.restitution);
            const bool exact_sphere = collider.shape == sim::ColliderShape::Sphere;
            ifps_count_edit_->setEnabled(!exact_sphere);
            ifps_count_edit_->setToolTip(exact_sphere
                ? "Analytic sphere colliders use one exact sphere"
                : "Positive leaf-sphere count. Large values significantly increase offline build time.");
            const std::uint32_t effective_count = collider.ifps_sphere_count == 0
                ? 256U : collider.ifps_sphere_count;
            ifps_count_edit_->setValue(static_cast<int>(exact_sphere ? 1U : effective_count));
            update_ifps_level_controls(entity);
        } else {
            for (auto* field : friction_edit_) field->setValue(0.0);
            restitution_edit_->setValue(0.0);
        }
        const bool has_camera = registry.has<sim::CameraComponent>(entity) &&
                                registry.has<sim::SensorComponent>(entity);
        camera_widget_->setEnabled(has_camera);
        if (has_camera) {
            const auto& sensor = registry.get<sim::SensorComponent>(entity);
            const auto& camera = registry.get<sim::CameraComponent>(entity);
            camera_type_edit_->setCurrentIndex(camera.output == sim::CameraOutput::RGBD ? 1 : 0);
            camera_frame_edit_->setText(QString::fromStdString(sensor.frame_id));
            camera_rate_edit_->setValue(sensor.update_rate_hz);
            camera_width_edit_->setValue(static_cast<int>(camera.width));
            camera_height_edit_->setValue(static_cast<int>(camera.height));
            camera_fov_edit_->setValue(camera.vertical_fov_radians *
                                       180.0 / 3.14159265358979323846);
            camera_near_edit_->setValue(camera.near_plane);
            camera_far_edit_->setValue(camera.far_plane);
            camera_preview_edit_->setItemData(1,
                camera.output == sim::CameraOutput::RGBD ? QVariant{} : QVariant(0),
                Qt::UserRole - 1);
            if (camera.output == sim::CameraOutput::RGB &&
                camera_preview_edit_->currentIndex() == 1)
                camera_preview_edit_->setCurrentIndex(0);
        }
        const auto parent = registry.get<sim::HierarchyComponent>(entity).parent;
        parent_label_->setText(parent == sim::kInvalidEntity ? "World root" :
            QString("%1 [%2]").arg(QString::fromStdString(
                registry.get<sim::NameComponent>(parent).name)).arg(parent));
        QString components = "Transform, Hierarchy";
        if (registry.has<sim::RobotComponent>(entity)) components += ", Robot";
        if (registry.has<sim::JointComponent>(entity)) components += ", Joint";
        if (registry.has<sim::VisualComponent>(entity)) components += ", Visual";
        if (registry.has<sim::ColliderComponent>(entity)) components += ", Collider";
        if (registry.has<sim::RigidBodyComponent>(entity)) components += ", RigidBody";
        if (registry.has<sim::SensorComponent>(entity)) components += ", Sensor";
        if (registry.has<sim::CameraComponent>(entity))
            components += registry.get<sim::CameraComponent>(entity).output ==
                sim::CameraOutput::RGBD ? ", RGBD Camera" : ", RGB Camera";
        if (registry.has<sim::LightComponent>(entity)) {
            const auto type = registry.get<sim::LightComponent>(entity).type;
            components += type == sim::LightType::Point ? ", Point Light" :
                type == sim::LightType::Spot ? ", Spotlight" :
                type == sim::LightType::Directional ? ", Directional Light" : ", Ambient Light";
        }
        if (robot_managed) components += " (robot-managed)";
        components_label_->setText(QString("Entity %1\n%2").arg(entity).arg(components));
        inspector_updating_ = false;
        (void)item;
    }

    void refresh_dynamic_inspector() {
        const auto selected = selected_entity();
        if (!selected || !world_->registry().has<sim::JointComponent>(*selected)) return;
        QWidget* focused = QApplication::focusWidget();
        if (focused && (focused == inspector_widget_ || inspector_widget_->isAncestorOf(focused)))
            return;
        inspect(hierarchy_->currentItem());
    }

    void apply_inspector() {
        if (inspector_updating_) return;
        const auto selected = selected_entity();
        if (!selected) return;
        const auto entity = *selected;
        auto& registry = world_->registry();
        registry.get<sim::NameComponent>(entity).name = name_edit_->text().toStdString();
        auto& transform = registry.get<sim::TransformComponent>(entity).local;
        transform.translation = {position_edit_[0]->value(), position_edit_[1]->value(),
                                 position_edit_[2]->value()};
        transform.rotation = EulerDegrees(rotation_edit_[0]->value(), rotation_edit_[1]->value(),
                                          rotation_edit_[2]->value());
        transform.scale = {scale_edit_[0]->value(), scale_edit_[1]->value(), scale_edit_[2]->value()};
        if (registry.has<sim::VisualComponent>(entity)) {
            auto& visual = registry.get<sim::VisualComponent>(entity);
            visual.visible = visible_edit_->isChecked();
            visual.min_lod = static_cast<std::uint8_t>(min_lod_edit_->value());
            visual.max_lod = static_cast<std::uint8_t>(
                std::max(min_lod_edit_->value(), max_lod_edit_->value()));
            if (max_lod_edit_->value() != visual.max_lod)
                max_lod_edit_->setValue(visual.max_lod);
        }
        if (registry.has<sim::LightComponent>(entity)) {
            auto& light = registry.get<sim::LightComponent>(entity);
            light.type = static_cast<sim::LightType>(light_type_edit_->currentIndex());
            light.enabled = light_enabled_edit_->isChecked();
            light.color = {static_cast<float>(light_color_.redF()),
                           static_cast<float>(light_color_.greenF()),
                           static_cast<float>(light_color_.blueF())};
            light.intensity = static_cast<float>(light_intensity_edit_->value());
            light.range = static_cast<float>(light_range_edit_->value());
            light.decay = static_cast<float>(light_decay_edit_->value());
            constexpr double radians = 3.14159265358979323846 / 180.0;
            const double inner = light_inner_cone_edit_->value();
            const double outer = std::max(inner, light_outer_cone_edit_->value());
            light.inner_cone_radians = static_cast<float>(inner * radians);
            light.outer_cone_radians = static_cast<float>(outer * radians);
            light.angular_diameter_radians = static_cast<float>(
                light_angular_diameter_edit_->value() * radians);
            if (outer != light_outer_cone_edit_->value())
                light_outer_cone_edit_->setValue(outer);
        }
        world_->update_world_transforms();
        rebuild_active_backends();
        world_->capture_initial_state();
        if (entity_items_.contains(entity))
            entity_items_.at(entity)->setText(0, QString("%1  [%2]").arg(name_edit_->text()).arg(entity));
        render_viewport();
    }

    void apply_rigid_body_inspector() {
        if (inspector_updating_) return;
        const auto selected = selected_entity();
        if (!selected) return;
        auto& registry = world_->registry();
        const auto entity = *selected;
        const bool robot_managed = robot_owner(entity).has_value();
        if (!robot_managed) {
            if (!body_enabled_edit_->isChecked()) {
                registry.remove<sim::RigidBodyComponent>(entity);
            } else {
                if (!registry.has<sim::RigidBodyComponent>(entity))
                    registry.emplace<sim::RigidBodyComponent>(entity,
                        sim::RigidBodyComponent{1.0, {}, {}, false, true});
                auto& body = registry.get<sim::RigidBodyComponent>(entity);
                body.dynamic = body_type_edit_->currentIndex() == 1;
                body.gravity_enabled = gravity_edit_->isChecked();
                body.mass = mass_edit_->value();
            }
        }
        if (registry.has<sim::ColliderComponent>(entity)) {
            auto& collider = registry.get<sim::ColliderComponent>(entity);
            collider.friction = {friction_edit_[0]->value(), friction_edit_[1]->value(),
                                 friction_edit_[2]->value()};
            collider.restitution = restitution_edit_->value();
        }
        const bool dynamic = !robot_managed && registry.has<sim::RigidBodyComponent>(entity) &&
                             registry.get<sim::RigidBodyComponent>(entity).dynamic;
        if (dynamic && physics_backend_->currentText() == "None") {
#if defined(SIM_HAS_MUJOCO)
            physics_backend_->setCurrentText("MuJoCo");
            apply_backend_configuration(false);
            log("MuJoCo physics enabled for dynamic body simulation");
#elif defined(SIM_HAS_BULLET_PLUGIN)
            physics_backend_->setCurrentText("Bullet3");
            apply_backend_configuration(false);
            log("Bullet3 physics enabled for dynamic body simulation");
#else
            log("Dynamic body requires a build with a physics backend");
#endif
        } else {
            rebuild_active_backends();
        }
        world_->capture_initial_state();
        inspect(hierarchy_->currentItem());
        render_dirty_ = true;
        render_viewport();
    }

    void apply_camera_inspector() {
        if (inspector_updating_) return;
        const auto selected = selected_entity();
        if (!selected) return;
        auto& registry = world_->registry();
        if (!registry.has<sim::SensorComponent>(*selected) ||
            !registry.has<sim::CameraComponent>(*selected)) return;
        if (camera_far_edit_->value() <= camera_near_edit_->value()) {
            camera_far_edit_->setValue(camera_near_edit_->value() + 0.001);
        }
        auto& sensor = registry.get<sim::SensorComponent>(*selected);
        auto& camera = registry.get<sim::CameraComponent>(*selected);
        sensor.frame_id = camera_frame_edit_->text().trimmed().toStdString();
        sensor.update_rate_hz = camera_rate_edit_->value();
        sensor.next_update_time = world_->time().state().simulation_time;
        camera.width = static_cast<std::uint32_t>(camera_width_edit_->value());
        camera.height = static_cast<std::uint32_t>(camera_height_edit_->value());
        camera.vertical_fov_radians = camera_fov_edit_->value() *
                                      3.14159265358979323846 / 180.0;
        camera.near_plane = camera_near_edit_->value();
        camera.far_plane = camera_far_edit_->value();
        camera.output = camera_type_edit_->currentIndex() == 1
            ? sim::CameraOutput::RGBD : sim::CameraOutput::RGB;
        if (camera.output == sim::CameraOutput::RGB &&
            camera_preview_edit_->currentIndex() == 1)
            camera_preview_edit_->setCurrentIndex(0);
        world_->capture_initial_state();
        inspect(hierarchy_->currentItem());
        render_sensor_preview();
    }

    void render_sensor_preview() {
#if defined(SIM_HAS_VULKAN)
        if (!renderer_) return;
        const auto selected = selected_entity();
        if (!selected || !world_->registry().has<sim::CameraComponent>(*selected)) {
            sensor_view_->setText("Select an RGB or RGBD camera sensor to preview its output.");
            return;
        }
        try {
            auto* preview_renderer = sensor_renderer_ ? sensor_renderer_.get() : renderer_.get();
            preview_renderer->sync_from_scene(*world_);
            const auto& camera = world_->registry().get<sim::CameraComponent>(*selected);
            const auto& pose = world_->registry().get<sim::TransformComponent>(*selected).world;
            std::vector<std::uint8_t> rgb;
            std::vector<float> depth;
            preview_renderer->render_camera(pose, camera, rgb, depth);
            QImage frame;
            if (camera_preview_edit_->currentIndex() == 1 &&
                camera.output == sim::CameraOutput::RGBD) {
                frame = QImage(static_cast<int>(camera.width), static_cast<int>(camera.height),
                               QImage::Format_Grayscale8);
                const double range = std::max(1.0e-9, camera.far_plane - camera.near_plane);
                for (std::uint32_t y = 0; y < camera.height; ++y) {
                    auto* row = frame.scanLine(static_cast<int>(y));
                    for (std::uint32_t x = 0; x < camera.width; ++x) {
                        const float meters = depth[static_cast<std::size_t>(y) * camera.width + x];
                        const double normalized = std::clamp(
                            (static_cast<double>(meters) - camera.near_plane) / range, 0.0, 1.0);
                        row[x] = static_cast<uchar>(std::lround((1.0 - normalized) * 255.0));
                    }
                }
            } else {
                frame = QImage(rgb.data(), static_cast<int>(camera.width),
                               static_cast<int>(camera.height), static_cast<int>(camera.width * 3),
                               QImage::Format_RGB888).copy();
            }
            sensor_view_->setPixmap(QPixmap::fromImage(frame).scaled(
                sensor_view_->size(), Qt::KeepAspectRatio, Qt::SmoothTransformation));
        } catch (const std::exception& error) {
            sensor_view_->setText(QString("Sensor render error: %1").arg(error.what()));
        }
#endif
    }

    void delete_selected_entity() {
        const auto selected = selected_entity();
        if (!selected) return;
        const auto name = QString::fromStdString(
            world_->registry().get<sim::NameComponent>(*selected).name);
        if (QMessageBox::question(this, "Delete entity",
                                  QString("Delete '%1' and all of its children?").arg(name)) !=
            QMessageBox::Yes) return;
        world_->destroy_entity(*selected);
        refresh_scene();
        render_viewport();
    }

    [[nodiscard]] sim::Transform camera_pose() const {
        sim::Transform pose;
        pose.rotation = EulerDegrees(0.0, camera_elevation_ * 180.0 / 3.14159265358979323846,
                                     camera_yaw_ * 180.0 / 3.14159265358979323846);
        const sim::Vec3 forward = pose.rotation.rotate({1.0, 0.0, 0.0});
        pose.translation = camera_target_ - forward * camera_distance_;
        return pose;
    }

    void frame_scene() {
#if defined(SIM_HAS_VULKAN)
        if (!renderer_) return;
        renderer_->sync_from_scene(*world_);
        const auto& snapshot = renderer_->snapshot();
        if (!snapshot.has_bounds) return;
        camera_target_ = (snapshot.bounds_min + snapshot.bounds_max) * 0.5;
        const sim::Vec3 extent = snapshot.bounds_max - snapshot.bounds_min;
        const double radius = std::max({extent.x, extent.y, extent.z}) * 0.5;
        camera_distance_ = std::max(0.05, radius * 2.4);
        camera_yaw_ = 0.0;
        camera_elevation_ = 0.28;
        render_viewport();
#endif
    }

    [[nodiscard]] bool is_descendant(sim::EntityId entity, sim::EntityId ancestor) const {
        while (entity != sim::kInvalidEntity && world_->registry().valid(entity)) {
            if (entity == ancestor) return true;
            entity = world_->registry().get<sim::HierarchyComponent>(entity).parent;
        }
        return false;
    }

    [[nodiscard]] std::optional<std::pair<sim::Vec3, sim::Vec3>> selection_bounds() const {
#if defined(SIM_HAS_VULKAN)
        const auto selected = selected_entity();
        if (!renderer_ || !selected) return std::nullopt;
        bool found = false;
        sim::Vec3 minimum{std::numeric_limits<double>::max(), std::numeric_limits<double>::max(),
                          std::numeric_limits<double>::max()};
        sim::Vec3 maximum{std::numeric_limits<double>::lowest(), std::numeric_limits<double>::lowest(),
                          std::numeric_limits<double>::lowest()};
        for (const auto& instance : renderer_->snapshot().instances) {
            if (!is_descendant(instance.entity, *selected)) continue;
            minimum = {std::min(minimum.x, instance.bounds_min.x), std::min(minimum.y, instance.bounds_min.y),
                       std::min(minimum.z, instance.bounds_min.z)};
            maximum = {std::max(maximum.x, instance.bounds_max.x), std::max(maximum.y, instance.bounds_max.y),
                       std::max(maximum.z, instance.bounds_max.z)};
            found = true;
        }
        if (found) return std::pair{minimum, maximum};
        if (world_->registry().has<sim::LightComponent>(*selected)) {
            const auto center = world_->registry().get<sim::TransformComponent>(*selected).world.translation;
            const double radius = std::max(0.08, camera_distance_ * 0.012);
            return std::pair{center - sim::Vec3{radius, radius, radius},
                             center + sim::Vec3{radius, radius, radius}};
        }
        if (world_->registry().has<sim::CameraComponent>(*selected) &&
            world_->registry().has<sim::TransformComponent>(*selected)) {
            const auto& transform = world_->registry().get<sim::TransformComponent>(*selected).world;
            const auto& camera = world_->registry().get<sim::CameraComponent>(*selected);
            const double display_far = std::min(
                camera.far_plane, std::max(camera.near_plane * 5.0, camera_distance_ * 0.5));
            const double half_height = display_far * std::tan(camera.vertical_fov_radians * 0.5);
            const double half_width = half_height *
                (static_cast<double>(camera.width) / std::max(1.0, static_cast<double>(camera.height)));
            const double radius = std::max(0.08, std::sqrt(
                display_far * display_far + half_width * half_width + half_height * half_height));
            const auto center = transform.translation;
            return std::pair{center - sim::Vec3{radius, radius, radius},
                             center + sim::Vec3{radius, radius, radius}};
        }
#endif
        return std::nullopt;
    }

    void focus_selection() {
        const auto bounds = selection_bounds();
        if (!bounds) return;
        camera_target_ = (bounds->first + bounds->second) * 0.5;
        const auto extent = bounds->second - bounds->first;
        camera_distance_ = std::max(0.05, std::max({extent.x, extent.y, extent.z}) * 1.2);
        render_viewport();
    }

    void move_selected(const sim::Vec3& world_delta) {
        const auto selected = selected_entity();
        if (!selected) return;
        auto& transform = world_->registry().get<sim::TransformComponent>(*selected);
        const auto parent = world_->registry().get<sim::HierarchyComponent>(*selected).parent;
        if (parent == sim::kInvalidEntity) {
            transform.local.translation = transform.local.translation + world_delta;
        } else {
            sim::Transform desired_world = transform.world;
            desired_world.translation = desired_world.translation + world_delta;
            transform.local = world_->registry().get<sim::TransformComponent>(parent).world.inverse() *
                              desired_world;
        }
        world_->update_world_transforms();
        inspect(hierarchy_->currentItem());
    }

    void handle_viewport_key(int key) {
        if (key == Qt::Key_F) {
            focus_selection();
            return;
        }
        const auto pose = camera_pose();
        const sim::Vec3 forward = pose.rotation.rotate({1.0, 0.0, 0.0});
        const sim::Vec3 left = pose.rotation.rotate({0.0, 1.0, 0.0});
        const sim::Vec3 up{0.0, 0.0, 1.0};
        const double step = std::max(0.01, camera_distance_ * 0.08);
        sim::Vec3 movement{};
        if (key == Qt::Key_W) movement = forward * step;
        else if (key == Qt::Key_S) movement = forward * -step;
        else if (key == Qt::Key_A) movement = left * step;
        else if (key == Qt::Key_D) movement = left * -step;
        else if (key == Qt::Key_Q) movement = up * -step;
        else if (key == Qt::Key_E) movement = up * step;
        else return;
        camera_target_ = camera_target_ + movement;
        render_viewport();
    }

    void manipulate_viewport(const QPoint& delta, const QPoint& position,
                             Qt::MouseButtons buttons, Qt::KeyboardModifiers modifiers) {
        if ((buttons & Qt::LeftButton) && selected_entity() && gizmo_drag_active_) {
            gizmo_transform_changed_ = true;
            const auto pose = camera_pose();
            const sim::Vec3 left = pose.rotation.rotate({0.0, 1.0, 0.0});
            const sim::Vec3 up = pose.rotation.rotate({0.0, 0.0, 1.0});
            const double units_per_pixel = camera_distance_ * 0.0025;
            if (transform_tool_ == TransformTool::Move) {
                move_selected(left * (-delta.x() * units_per_pixel) +
                              up * (-delta.y() * units_per_pixel));
            } else if (transform_tool_ == TransformTool::Rotate) {
                auto& transform = world_->registry().get<sim::TransformComponent>(*selected_entity()).local;
                auto euler = QuaternionEulerDegrees(transform.rotation);
                euler.y += delta.y() * 0.35;
                euler.z -= delta.x() * 0.35;
                transform.rotation = EulerDegrees(euler.x, euler.y, euler.z);
                world_->update_world_transforms();
                inspect(hierarchy_->currentItem());
            } else {
                auto& scale = world_->registry().get<sim::TransformComponent>(*selected_entity()).local.scale;
                const double factor = std::exp((delta.x() - delta.y()) * 0.01);
                scale = {std::clamp(scale.x * factor, 1.0e-5, 1.0e6),
                         std::clamp(scale.y * factor, 1.0e-5, 1.0e6),
                         std::clamp(scale.z * factor, 1.0e-5, 1.0e6)};
                world_->update_world_transforms();
                inspect(hierarchy_->currentItem());
            }
        } else if (buttons & Qt::LeftButton) {
            camera_yaw_ -= delta.x() * 0.008;
            camera_elevation_ = std::clamp(camera_elevation_ + delta.y() * 0.006, -1.45, 1.45);
        } else if ((buttons & Qt::MiddleButton) || (buttons & Qt::RightButton)) {
            const auto pose = camera_pose();
            const sim::Vec3 left = pose.rotation.rotate({0.0, 1.0, 0.0});
            const sim::Vec3 up = pose.rotation.rotate({0.0, 0.0, 1.0});
            const double units_per_pixel = camera_distance_ * 0.0025;
            camera_target_ = camera_target_ + left * (delta.x() * units_per_pixel) +
                             up * (delta.y() * units_per_pixel);
        }
        (void)position;
        (void)modifiers;
        render_viewport();
    }

    void pick_entity(const QPoint& position) {
#if defined(SIM_HAS_VULKAN)
        const QPixmap* pixmap = viewport_->pixmap();
        if (!renderer_ || pixmap == nullptr || pixmap->width() <= 0 || pixmap->height() <= 0)
            return;
        const QPoint local = position - viewport_pixmap_offset_;
        if (!QRect(QPoint{}, pixmap->size()).contains(local)) {
            hierarchy_->setCurrentItem(nullptr);
            hierarchy_->clearSelection();
            return;
        }
        double light_distance = std::numeric_limits<double>::max();
        sim::EntityId light_hit = sim::kInvalidEntity;
        for (const auto entity : world_->registry().view<sim::LightComponent,
                                                          sim::TransformComponent>()) {
            const auto projected = project_point(
                world_->registry().get<sim::TransformComponent>(entity).world.translation,
                pixmap->size());
            if (!projected) continue;
            const double dx = projected->x() - local.x();
            const double dy = projected->y() - local.y();
            const double distance_squared = dx * dx + dy * dy;
            if (distance_squared <= 14.0 * 14.0 && distance_squared < light_distance) {
                light_distance = distance_squared;
                light_hit = entity;
            }
        }
        if (light_hit != sim::kInvalidEntity && entity_items_.contains(light_hit)) {
            hierarchy_->setCurrentItem(entity_items_.at(light_hit));
            hierarchy_->scrollToItem(entity_items_.at(light_hit));
            return;
        }
        const double u = static_cast<double>(local.x()) / pixmap->width();
        const double v = static_cast<double>(local.y()) / pixmap->height();
        const double aspect = static_cast<double>(pixmap->width()) / pixmap->height();
        const double tangent = std::tan(0.5 * 1.0471975511965976);
        sim::Vec3 direction{1.0, -(2.0 * u - 1.0) * tangent * aspect,
                           (1.0 - 2.0 * v) * tangent};
        const double length = std::sqrt(direction.x * direction.x + direction.y * direction.y +
                                        direction.z * direction.z);
        direction = direction * (1.0 / length);
        const auto pose = camera_pose();
        direction = pose.rotation.rotate(direction);
        double nearest = std::numeric_limits<double>::max();
        sim::EntityId hit = sim::kInvalidEntity;
        for (const auto& instance : renderer_->snapshot().instances) {
            double enter = 0.0;
            double exit = std::numeric_limits<double>::max();
            const std::array<double, 3> origin{pose.translation.x, pose.translation.y,
                                               pose.translation.z};
            const std::array<double, 3> ray{direction.x, direction.y, direction.z};
            const std::array<double, 3> minimum{instance.bounds_min.x, instance.bounds_min.y,
                                                instance.bounds_min.z};
            const std::array<double, 3> maximum{instance.bounds_max.x, instance.bounds_max.y,
                                                instance.bounds_max.z};
            bool intersects = true;
            for (int axis = 0; axis < 3; ++axis) {
                if (std::abs(ray[axis]) < 1.0e-12) {
                    intersects = origin[axis] >= minimum[axis] && origin[axis] <= maximum[axis];
                } else {
                    double first = (minimum[axis] - origin[axis]) / ray[axis];
                    double second = (maximum[axis] - origin[axis]) / ray[axis];
                    if (first > second) std::swap(first, second);
                    enter = std::max(enter, first);
                    exit = std::min(exit, second);
                    intersects = enter <= exit;
                }
                if (!intersects) break;
            }
            if (intersects && enter < nearest) {
                nearest = enter;
                hit = instance.entity;
            }
        }
        if (hit != sim::kInvalidEntity && entity_items_.contains(hit)) {
            hierarchy_->setCurrentItem(entity_items_.at(hit));
            hierarchy_->scrollToItem(entity_items_.at(hit));
        } else {
            hierarchy_->setCurrentItem(nullptr);
            hierarchy_->clearSelection();
        }
#else
        (void)position;
#endif
    }

    [[nodiscard]] std::optional<QPointF> project_point(const sim::Vec3& point,
                                                       const QSize& size) const {
        if (size.width() <= 0 || size.height() <= 0) return std::nullopt;
        const auto camera_space = camera_pose().inverse() * sim::Transform{point};
        if (camera_space.translation.x <= 1.0e-6) return std::nullopt;
        const double tangent = std::tan(0.5 * 1.0471975511965976);
        const double aspect = static_cast<double>(size.width()) / size.height();
        const double horizontal = -camera_space.translation.y /
                                  (camera_space.translation.x * tangent * aspect);
        const double vertical = camera_space.translation.z /
                                (camera_space.translation.x * tangent);
        return QPointF((horizontal * 0.5 + 0.5) * size.width(),
                       (0.5 - vertical * 0.5) * size.height());
    }

    void draw_world_line(QPainter& painter, const QSize& size, const sim::Vec3& first,
                         const sim::Vec3& second) const {
        const auto a = project_point(first, size);
        const auto b = project_point(second, size);
        if (a && b) painter.drawLine(*a, *b);
    }

    void draw_camera_frustum(QPainter& painter, const QSize& size, sim::EntityId entity) const {
        const auto& registry = world_->registry();
        const auto& camera = registry.get<sim::CameraComponent>(entity);
        const auto& pose = registry.get<sim::TransformComponent>(entity).world;
        const double aspect = static_cast<double>(camera.width) /
                              std::max(1.0, static_cast<double>(camera.height));
        const double tangent = std::tan(camera.vertical_fov_radians * 0.5);
        const double display_far = std::min(
            camera.far_plane, std::max(camera.near_plane * 5.0, camera_distance_ * 0.5));
        const auto world_point = [&](double distance, double horizontal, double vertical) {
            return pose.translation + pose.rotation.rotate({distance, horizontal, vertical});
        };
        const auto plane = [&](double distance) {
            const double half_height = distance * tangent;
            const double half_width = half_height * aspect;
            return std::array<sim::Vec3, 4>{
                world_point(distance, -half_width, -half_height),
                world_point(distance, half_width, -half_height),
                world_point(distance, half_width, half_height),
                world_point(distance, -half_width, half_height)};
        };
        const auto near_corners = plane(camera.near_plane);
        const auto far_corners = plane(display_far);
        constexpr std::array<std::array<int, 2>, 4> rectangle_edges{{
            {{0, 1}}, {{1, 2}}, {{2, 3}}, {{3, 0}}
        }};

        painter.setBrush(Qt::NoBrush);
        painter.setPen(QPen(QColor(55, 220, 235, 230), 1.8));
        for (const auto& corner : far_corners)
            draw_world_line(painter, size, pose.translation, corner);
        for (const auto& edge : rectangle_edges)
            draw_world_line(painter, size, far_corners[edge[0]], far_corners[edge[1]]);

        painter.setPen(QPen(QColor(115, 240, 250, 210), 1.4, Qt::DashLine));
        for (const auto& edge : rectangle_edges)
            draw_world_line(painter, size, near_corners[edge[0]], near_corners[edge[1]]);

        const auto axis_end = world_point(display_far, 0.0, 0.0);
        painter.setPen(QPen(QColor(255, 220, 70, 240), 2.2));
        draw_world_line(painter, size, pose.translation, axis_end);
        if (const auto label = project_point(axis_end, size)) {
            painter.setPen(QColor(170, 250, 255));
            QString description = QString("FOV %1 deg | range %2-%3 m")
                .arg(camera.vertical_fov_radians * 180.0 / 3.14159265358979323846,
                     0, 'f', 1)
                .arg(camera.near_plane, 0, 'g', 3)
                .arg(camera.far_plane, 0, 'g', 3);
            if (display_far < camera.far_plane)
                description += QString(" | drawn %1 m").arg(display_far, 0, 'g', 3);
            painter.drawText(*label + QPointF(8.0, -8.0), description);
        }
    }

    [[nodiscard]] sim::Vec3 collider_point(const sim::Transform& transform,
                                           const sim::Vec3& local) const {
        sim::Transform point;
        point.translation = local;
        return (transform * point).translation;
    }

    [[nodiscard]] std::string visual_mesh_for_collider(sim::EntityId entity) const {
        const auto& registry = world_->registry();
        if (registry.has<sim::VisualComponent>(entity)) {
            const auto& resource = registry.get<sim::VisualComponent>(entity).mesh_resource;
            if (!resource.empty()) return resource;
        }
        if (!registry.has<sim::HierarchyComponent>(entity)) return {};
        const auto parent = registry.get<sim::HierarchyComponent>(entity).parent;
        if (parent == sim::kInvalidEntity || !registry.has<sim::HierarchyComponent>(parent))
            return {};
        if (registry.has<sim::VisualComponent>(parent)) {
            const auto& resource = registry.get<sim::VisualComponent>(parent).mesh_resource;
            if (!resource.empty()) return resource;
        }
        for (const auto sibling : registry.get<sim::HierarchyComponent>(parent).children) {
            if (!registry.has<sim::VisualComponent>(sibling)) continue;
            const auto& resource = registry.get<sim::VisualComponent>(sibling).mesh_resource;
            if (!resource.empty()) return resource;
        }
        return {};
    }

    [[nodiscard]] const CollisionWireframe* collision_wireframe(
        const std::string& resource) {
        if (resource.empty()) return nullptr;
        const auto cached = collision_wireframes_.find(resource);
        if (cached != collision_wireframes_.end())
            return cached->second ? &*cached->second : nullptr;

        std::optional<CollisionWireframe> result;
#if defined(SIM_HAS_ASSIMP)
        try {
            std::string source = resource;
            std::optional<std::size_t> selected_mesh;
            if (const auto marker = resource.rfind("#mesh="); marker != std::string::npos) {
                source = resource.substr(0, marker);
                selected_mesh = static_cast<std::size_t>(
                    std::stoull(resource.substr(marker + 6)));
            }
            auto asset = sim::AssimpImporter{}.import(PathFromUtf8(source)).asset;
            sim::AssetValidator::normalize(asset);

            CollisionWireframe wireframe;
            std::unordered_set<std::uint64_t> unique_edges;
            const auto append_edge = [&](std::uint32_t first, std::uint32_t second) {
                if (first > second) std::swap(first, second);
                const std::uint64_t key = (static_cast<std::uint64_t>(first) << 32U) | second;
                if (unique_edges.insert(key).second)
                    wireframe.edges.push_back({first, second});
            };
            for (std::size_t mesh_index = 0; mesh_index < asset.meshes.size(); ++mesh_index) {
                if (selected_mesh && *selected_mesh != mesh_index) continue;
                // A complete coarse topology remains visually continuous; randomly
                // sampling fine-mesh triangles produced the former broken-line effect.
                const auto mesh = sim::GenerateMeshLod(asset.meshes[mesh_index], 7);
                const auto offset = static_cast<std::uint32_t>(wireframe.vertices.size());
                wireframe.vertices.insert(wireframe.vertices.end(), mesh.positions.begin(),
                                          mesh.positions.end());
                for (std::size_t index = 0; index + 2 < mesh.indices.size(); index += 3) {
                    const auto first = offset + mesh.indices[index];
                    const auto second = offset + mesh.indices[index + 1];
                    const auto third = offset + mesh.indices[index + 2];
                    if (first >= wireframe.vertices.size() ||
                        second >= wireframe.vertices.size() ||
                        third >= wireframe.vertices.size())
                        continue;
                    append_edge(first, second);
                    append_edge(second, third);
                    append_edge(third, first);
                }
            }
            if (!wireframe.vertices.empty() && !wireframe.edges.empty())
                result = std::move(wireframe);
        } catch (const std::exception&) {
            // Cache failures too; a visual mesh may still provide a useful fallback.
        }
#else
        (void)resource;
#endif
        const auto [inserted, unused] = collision_wireframes_.emplace(resource,
                                                                       std::move(result));
        return inserted->second ? &*inserted->second : nullptr;
    }

    void draw_collider_overlay(QPainter& painter, const QSize& size, sim::EntityId entity) {
        const auto& registry = world_->registry();
        const auto& collider = registry.get<sim::ColliderComponent>(entity);
        const auto& transform = registry.get<sim::TransformComponent>(entity).world;
        QColor color(70, 230, 120, 210);
        bool dynamic_body = registry.has<sim::RigidBodyComponent>(entity) &&
                            registry.get<sim::RigidBodyComponent>(entity).dynamic;
        // Imported mesh assets keep the dynamic body on their top-level root while
        // the triangle collider lives on a visual descendant. Inherit the dynamic
        // display state through the hierarchy so the overlay matches physics.
        auto ancestor = registry.get<sim::HierarchyComponent>(entity).parent;
        while (!dynamic_body && ancestor != sim::kInvalidEntity && registry.valid(ancestor)) {
            dynamic_body = registry.has<sim::RigidBodyComponent>(ancestor) &&
                           registry.get<sim::RigidBodyComponent>(ancestor).dynamic;
            ancestor = registry.get<sim::HierarchyComponent>(ancestor).parent;
        }
        if (dynamic_body)
            color = QColor(40, 205, 255, 220);
        if (collider.is_trigger) color = QColor(245, 80, 225, 220);
        painter.setPen(QPen(color, 1.6, Qt::DashLine));
        painter.setBrush(Qt::NoBrush);

        const auto draw_box = [&](const sim::Vec3& half_size) {
            std::array<sim::Vec3, 8> corners{};
            for (int corner = 0; corner < 8; ++corner) {
                const sim::Vec3 local{(corner & 1) ? half_size.x : -half_size.x,
                                      (corner & 2) ? half_size.y : -half_size.y,
                                      (corner & 4) ? half_size.z : -half_size.z};
                corners[corner] = collider_point(transform, local);
            }
            constexpr std::array<std::array<int, 2>, 12> edges{{
                {{0,1}}, {{0,2}}, {{0,4}}, {{1,3}}, {{1,5}}, {{2,3}},
                {{2,6}}, {{3,7}}, {{4,5}}, {{4,6}}, {{5,7}}, {{6,7}}
            }};
            for (const auto& edge : edges)
                draw_world_line(painter, size, corners[edge[0]], corners[edge[1]]);
        };
        const auto draw_circle = [&](int first_axis, int second_axis, double radius,
                                     double z_offset) {
            constexpr int segments = 32;
            sim::Vec3 previous{};
            for (int segment = 0; segment <= segments; ++segment) {
                const double angle = segment * 6.283185307179586 / segments;
                std::array<double, 3> value{0.0, 0.0, z_offset};
                value[static_cast<std::size_t>(first_axis)] = std::cos(angle) * radius;
                value[static_cast<std::size_t>(second_axis)] = std::sin(angle) * radius;
                const sim::Vec3 current = collider_point(
                    transform, {value[0], value[1], value[2]});
                if (segment > 0) draw_world_line(painter, size, previous, current);
                previous = current;
            }
        };

        switch (collider.shape) {
            case sim::ColliderShape::Box:
                draw_box(collider.size * 0.5);
                break;
            case sim::ColliderShape::Sphere:
                draw_circle(0, 1, collider.size.x, 0.0);
                draw_circle(0, 2, collider.size.x, 0.0);
                draw_circle(1, 2, collider.size.x, 0.0);
                break;
            case sim::ColliderShape::Cylinder:
            case sim::ColliderShape::Capsule: {
                const double radius = collider.size.x;
                const double half_length = collider.size.z * 0.5;
                draw_circle(0, 1, radius, -half_length);
                draw_circle(0, 1, radius, half_length);
                for (int side = 0; side < 4; ++side) {
                    const double angle = side * 1.5707963267948966;
                    const sim::Vec3 lower{std::cos(angle) * radius,
                                          std::sin(angle) * radius, -half_length};
                    const sim::Vec3 upper{lower.x, lower.y, half_length};
                    draw_world_line(painter, size, collider_point(transform, lower),
                                    collider_point(transform, upper));
                }
                if (collider.shape == sim::ColliderShape::Capsule) {
                    draw_world_line(painter, size,
                        collider_point(transform, {0.0, 0.0, -half_length - radius}),
                        collider_point(transform, {0.0, 0.0, -half_length}));
                    draw_world_line(painter, size,
                        collider_point(transform, {0.0, 0.0, half_length}),
                        collider_point(transform, {0.0, 0.0, half_length + radius}));
                }
                break;
            }
            case sim::ColliderShape::Mesh: {
                bool drawn = false;
                const auto draw_mesh = [&](const std::string& resource) {
                    const auto* wireframe = collision_wireframe(resource);
                    if (!wireframe) return false;
                    for (const auto& edge : wireframe->edges)
                        draw_world_line(painter, size,
                            collider_point(transform, wireframe->vertices[edge[0]]),
                            collider_point(transform, wireframe->vertices[edge[1]]));
                    return true;
                };
                // URDF collision geometry is authoritative. If it is absent or cannot
                // be decoded, use the visual mesh belonging to the same Link.
                if (!collider.mesh_resource.empty())
                    drawn = draw_mesh(collider.mesh_resource);
                if (!drawn) {
                    const auto visual_mesh = visual_mesh_for_collider(entity);
                    if (visual_mesh != collider.mesh_resource) drawn = draw_mesh(visual_mesh);
                }
                // Do not render ColliderComponent's default 1 m size as a real mesh box.
                // A tiny marker communicates missing geometry without misleading users.
                if (!drawn) draw_box({0.01, 0.01, 0.01});
                break;
            }
        }
    }

    [[nodiscard]] const sim::IFPSSphereTree& ifps_tree(sim::EntityId entity) {
        const auto found = ifps_tree_cache_.find(entity);
        if (found != ifps_tree_cache_.end()) return found->second;
        const auto& collider = world_->registry().get<sim::ColliderComponent>(entity);
        const std::uint32_t effective_count = collider.ifps_sphere_count == 0
            ? 256U : collider.ifps_sphere_count;
        const auto [inserted, unused] = ifps_tree_cache_.emplace(
            entity, ifps_build_cache_.get(collider, effective_count));
        return inserted->second;
    }

    void draw_ifps_overlay(QPainter& painter, const QSize& size, sim::EntityId entity) {
        const auto& registry = world_->registry();
        const auto& tree = ifps_tree(entity);
        const auto& transform = registry.get<sim::TransformComponent>(entity).world;
        const sim::Vec3 scale{std::abs(transform.scale.x), std::abs(transform.scale.y),
                              std::abs(transform.scale.z)};
        const double radius_scale = std::max({scale.x, scale.y, scale.z});
        const auto world_sphere = [&](std::uint32_t index) {
            const auto& sphere = tree.nodes[index];
            const sim::Vec3 scaled{sphere.center.x * scale.x, sphere.center.y * scale.y,
                                   sphere.center.z * scale.z};
            return std::pair{transform.translation + transform.rotation.rotate(scaled),
                             sphere.radius * radius_scale};
        };
        const std::array<QColor, 8> colors{{QColor(240, 75, 75), QColor(80, 215, 120),
            QColor(45, 190, 245), QColor(255, 190, 55), QColor(220, 90, 235),
            QColor(70, 225, 205), QColor(255, 125, 45), QColor(150, 135, 255)}};
        std::vector<std::vector<std::uint32_t>> levels{{tree.root}};
        while (levels.size() < ifps_level_edits_.size()) {
            std::vector<std::uint32_t> next;
            for (const auto index : levels.back()) {
                const auto& node = tree.nodes[index];
                for (std::uint32_t child = 0; child < node.child_count; ++child)
                    next.push_back(node.first_child + child);
            }
            if (next.empty()) break;
            levels.push_back(std::move(next));
        }

        constexpr int segments = 64;
        const auto camera = camera_pose();
        const auto sphere_silhouette = [&](const sim::Vec3& center, double radius) {
            QPainterPath path;
            const sim::Vec3 relative = center - camera.translation;
            const double distance_squared = relative.x * relative.x +
                relative.y * relative.y + relative.z * relative.z;
            if (distance_squared <= radius * radius) {
                path.addRect(QRectF(QPointF(0.0, 0.0), QSizeF(size)));
                return path;
            }
            const double distance = std::sqrt(distance_squared);
            const sim::Vec3 direction = relative * (1.0 / distance);
            const sim::Vec3 reference = std::abs(direction.z) < 0.9
                ? sim::Vec3{0.0, 0.0, 1.0} : sim::Vec3{0.0, 1.0, 0.0};
            const sim::Vec3 cross{direction.y * reference.z - direction.z * reference.y,
                                  direction.z * reference.x - direction.x * reference.z,
                                  direction.x * reference.y - direction.y * reference.x};
            const double cross_length = std::sqrt(
                cross.x * cross.x + cross.y * cross.y + cross.z * cross.z);
            const sim::Vec3 u = cross * (1.0 / cross_length);
            const sim::Vec3 v{direction.y * u.z - direction.z * u.y,
                              direction.z * u.x - direction.x * u.z,
                              direction.x * u.y - direction.y * u.x};
            const double ratio_squared = radius * radius / distance_squared;
            const sim::Vec3 circle_center = camera.translation + relative * (1.0 - ratio_squared);
            const double circle_radius = radius * std::sqrt(1.0 - ratio_squared);
            for (int segment = 0; segment <= segments; ++segment) {
                const double angle = segment * 6.283185307179586 / segments;
                const sim::Vec3 point = circle_center +
                    (u * std::cos(angle) + v * std::sin(angle)) * circle_radius;
                const auto projected = project_point(point, size);
                if (!projected) return QPainterPath{};
                if (segment == 0) path.moveTo(*projected);
                else path.lineTo(*projected);
            }
            path.closeSubpath();
            return path;
        };

        if (ifps_mode_edit_->currentIndex() == 0) {
            constexpr std::array<std::array<int, 2>, 3> planes{{{{0, 1}}, {{0, 2}}, {{1, 2}}}};
            for (std::size_t level = 0; level < levels.size(); ++level) {
                if (!ifps_level_edits_[level]->isChecked()) continue;
                painter.setPen(QPen(colors[level], 1.25));
                painter.setBrush(Qt::NoBrush);
                for (const auto index : levels[level]) {
                    const auto [center, radius] = world_sphere(index);
                    painter.drawPath(sphere_silhouette(center, radius));
                    for (const auto& plane : planes) {
                        sim::Vec3 previous{};
                        for (int segment = 0; segment <= segments; ++segment) {
                            const double angle = segment * 6.283185307179586 / segments;
                            std::array<double, 3> offset{};
                            offset[plane[0]] = std::cos(angle) * radius;
                            offset[plane[1]] = std::sin(angle) * radius;
                            const sim::Vec3 current = center +
                                sim::Vec3{offset[0], offset[1], offset[2]};
                            if (segment != 0)
                                draw_world_line(painter, size, previous, current);
                            previous = current;
                        }
                    }
                }
            }
            return;
        }

        struct SolidSphere { sim::Vec3 center; double radius; QColor color; double depth; };
        std::vector<SolidSphere> spheres;
        for (std::size_t level = 0; level < levels.size(); ++level) {
            if (!ifps_level_edits_[level]->isChecked()) continue;
            QColor color = colors[level];
            color.setAlpha(105);
            for (const auto index : levels[level]) {
                const auto [center, radius] = world_sphere(index);
                const sim::Vec3 delta = center - camera.translation;
                spheres.push_back({center, radius, color,
                    delta.x * delta.x + delta.y * delta.y + delta.z * delta.z});
            }
        }
        std::ranges::sort(spheres, std::greater{}, &SolidSphere::depth);
        for (const auto& sphere : spheres) {
            const QPainterPath silhouette = sphere_silhouette(sphere.center, sphere.radius);
            if (silhouette.isEmpty()) continue;
            painter.setPen(QPen(sphere.color.lighter(130), 1.0));
            painter.setBrush(sphere.color);
            painter.drawPath(silhouette);
        }
    }

    void decorate_viewport(QPixmap& pixels) {
        gizmo_visible_ = false;
        gizmo_hit_path_ = {};
        QPainter painter(&pixels);
        painter.setRenderHint(QPainter::Antialiasing);
        const auto selected_entity_id = selected_entity();
        for (const auto entity : world_->registry().view<sim::LightComponent,
                                                          sim::TransformComponent>()) {
            const auto& light = world_->registry().get<sim::LightComponent>(entity);
            const auto& transform = world_->registry().get<sim::TransformComponent>(entity).world;
            const auto center = project_point(transform.translation, pixels.size());
            if (!center) continue;
            QColor color(static_cast<int>(std::clamp(light.color[0], 0.0F, 1.0F) * 255.0F),
                         static_cast<int>(std::clamp(light.color[1], 0.0F, 1.0F) * 255.0F),
                         static_cast<int>(std::clamp(light.color[2], 0.0F, 1.0F) * 255.0F));
            if (!light.enabled) color = QColor(105, 105, 105);
            painter.setPen(QPen(color, 2.0));
            painter.setBrush(QColor(color.red(), color.green(), color.blue(), 180));
            painter.drawEllipse(*center, 8.0, 8.0);
            const auto direction = transform.rotation.rotate(
                light.type == sim::LightType::Spot ? sim::Vec3{0.0, 0.0, -1.0}
                                                   : sim::Vec3{1.0, 0.0, 0.0});
            const auto tip = project_point(transform.translation + direction * 0.35, pixels.size());
            if (tip) {
                painter.drawLine(*center, *tip);
                painter.drawLine(*tip, QPointF(tip->x() - 5.0, tip->y() - 3.0));
                painter.drawLine(*tip, QPointF(tip->x() - 5.0, tip->y() + 3.0));
            }
            if (selected_entity_id && *selected_entity_id == entity) {
                const auto& name = world_->registry().get<sim::NameComponent>(entity).name;
                painter.setPen(QColor(235, 245, 255));
                painter.drawText(QPointF(center->x() + 11.0, center->y() + 4.0),
                                 QString::fromStdString(name));

                const auto& light_transform = world_->registry().get<sim::TransformComponent>(entity).world;
                const double range = std::max(0.05, static_cast<double>(light.range));
                if (light.type == sim::LightType::Point) {
                    const auto range_point = project_point(
                        light_transform.translation + light_transform.rotation.rotate({0.0, range, 0.0}),
                        pixels.size());
                    if (range_point) {
                        const double radius = std::max(8.0, std::abs(range_point->x() - center->x()));
                        painter.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 150), 1.3,
                                            Qt::DashLine));
                        painter.setBrush(Qt::NoBrush);
                        painter.drawEllipse(*center, radius, radius);
                    }
                } else if (light.type == sim::LightType::Spot) {
                    constexpr double max_angle = 1.5676547341; // 89.8 degrees.
                    const double outer = std::clamp(
                        static_cast<double>(light.outer_cone_radians), 0.001, max_angle);
                    const double inner = std::clamp(
                        static_cast<double>(light.inner_cone_radians), 0.0, outer);
                    const auto draw_cone = [&](double angle, const QPen& pen) {
                        constexpr int segments = 32;
                        constexpr int sides = 4;
                        const double radius = range * std::tan(angle);
                        const auto cone_point = [&](double phase) {
                            return light_transform.translation + light_transform.rotation.rotate(
                                {std::cos(phase) * radius, std::sin(phase) * radius, -range});
                        };
                        painter.setPen(pen);
                        auto previous = cone_point(0.0);
                        for (int segment = 1; segment <= segments; ++segment) {
                            const auto current = cone_point(
                                static_cast<double>(segment) * 6.28318530717958647692 / segments);
                            draw_world_line(painter, pixels.size(), previous, current);
                            previous = current;
                        }
                        for (int side = 0; side < sides; ++side)
                            draw_world_line(painter, pixels.size(), light_transform.translation,
                                cone_point(static_cast<double>(side) * 6.28318530717958647692 / sides));
                    };
                    draw_cone(outer, QPen(QColor(color.red(), color.green(), color.blue(), 145),
                                          1.2, Qt::DashLine));
                    if (inner > 0.0)
                        draw_cone(inner, QPen(QColor(255, 210, 80, 195), 1.3, Qt::DashLine));
                }
            }
        }
        if (show_colliders_) {
            for (const auto entity : world_->registry().view<sim::ColliderComponent,
                                                              sim::TransformComponent>())
                draw_collider_overlay(painter, pixels.size(), entity);
        }
        const auto selected = selected_entity();
        if (ifps_show_edit_->isChecked() && selected &&
            world_->registry().has<sim::ColliderComponent>(*selected) &&
            world_->registry().has<sim::TransformComponent>(*selected))
            draw_ifps_overlay(painter, pixels.size(), *selected);
        if (selected && world_->registry().has<sim::CameraComponent>(*selected) &&
            world_->registry().has<sim::TransformComponent>(*selected))
            draw_camera_frustum(painter, pixels.size(), *selected);
        const auto bounds = selection_bounds();
        if (!bounds) return;
        const auto& minimum = bounds->first;
        const auto& maximum = bounds->second;
        std::array<sim::Vec3, 8> corners{};
        std::array<std::optional<QPointF>, 8> projected{};
        for (int corner = 0; corner < 8; ++corner) {
            corners[corner] = {(corner & 1) ? maximum.x : minimum.x,
                               (corner & 2) ? maximum.y : minimum.y,
                               (corner & 4) ? maximum.z : minimum.z};
            projected[corner] = project_point(corners[corner], pixels.size());
        }
        painter.setPen(QPen(QColor(255, 205, 40), 2.0));
        constexpr std::array<std::array<int, 2>, 12> edges{{
            {{0,1}}, {{0,2}}, {{0,4}}, {{1,3}}, {{1,5}}, {{2,3}},
            {{2,6}}, {{3,7}}, {{4,5}}, {{4,6}}, {{5,7}}, {{6,7}}
        }};
        for (const auto& edge : edges)
            if (projected[edge[0]] && projected[edge[1]])
                painter.drawLine(*projected[edge[0]], *projected[edge[1]]);

        const sim::Vec3 center = (minimum + maximum) * 0.5;
        const sim::Vec3 extent = maximum - minimum;
        const double axis_length = std::max(0.02, std::max({extent.x, extent.y, extent.z}) * 0.35);
        const auto screen_center = project_point(center, pixels.size());
        if (!screen_center) return;
        gizmo_center_ = screen_center->toPoint();
        gizmo_visible_ = true;
        QPainterPath handle_path;
        if (transform_tool_ == TransformTool::Rotate) {
            painter.setBrush(Qt::NoBrush);
            painter.setPen(QPen(QColor(235, 70, 70), 3.0));
            painter.drawEllipse(*screen_center, 42, 24);
            painter.setPen(QPen(QColor(70, 220, 100), 3.0));
            painter.drawEllipse(*screen_center, 24, 42);
            painter.setPen(QPen(QColor(75, 135, 255), 3.0));
            painter.drawEllipse(*screen_center, 42, 42);
            handle_path.addEllipse(*screen_center, 42, 24);
            handle_path.addEllipse(*screen_center, 24, 42);
            handle_path.addEllipse(*screen_center, 42, 42);
        } else {
            const std::array<sim::Vec3, 3> axes{{{axis_length, 0.0, 0.0},
                                                 {0.0, axis_length, 0.0},
                                                 {0.0, 0.0, axis_length}}};
            const std::array<QColor, 3> colors{{QColor(235, 70, 70), QColor(70, 220, 100),
                                                QColor(75, 135, 255)}};
            for (int axis = 0; axis < 3; ++axis) {
                const auto end = project_point(center + axes[axis], pixels.size());
                if (!end) continue;
                painter.setPen(QPen(colors[axis], 4.0));
                painter.drawLine(*screen_center, *end);
                handle_path.moveTo(*screen_center);
                handle_path.lineTo(*end);
                painter.setBrush(colors[axis]);
                if (transform_tool_ == TransformTool::Scale)
                    painter.drawRect(QRectF(end->x() - 5, end->y() - 5, 10, 10));
                else
                    painter.drawEllipse(*end, 5, 5);
            }
        }
        QPainterPathStroker stroker;
        stroker.setWidth(12.0);
        gizmo_hit_path_ = stroker.createStroke(handle_path);
        painter.setPen(QColor(255, 225, 100));
        painter.drawText(QPointF(screen_center->x() + 12, screen_center->y() - 12),
                         "F: focus | WASDQE: fly");
    }

    void render_viewport() {
#if defined(SIM_HAS_VULKAN)
        if (!renderer_) return;
        QElapsedTimer render_timer;
        render_timer.start();
        try {
            renderer_->sync_from_scene(*world_);
            if (sensor_renderer_) sensor_renderer_->sync_from_scene(*world_);
            for (const auto& diagnostic : renderer_->consume_diagnostics())
                log(QString::fromUtf8(diagnostic.c_str()));
            sim::CameraComponent camera;
            camera.width = 960;
            camera.height = std::max<std::uint32_t>(1, static_cast<std::uint32_t>(
                std::lround(camera.width * static_cast<double>(std::max(1, viewport_->height())) /
                            std::max(1, viewport_->width()))));
            camera.near_plane = std::max(0.001, camera_distance_ * 0.001);
            camera.far_plane = std::max(100.0, camera_distance_ * 20.0);
            const sim::Transform pose = camera_pose();
            std::vector<std::uint8_t> rgba;
            renderer_->render_camera_rgba(pose, camera, rgba);
            const QImage frame(rgba.data(), static_cast<int>(camera.width),
                               static_cast<int>(camera.height),
                               static_cast<int>(camera.width * 4), QImage::Format_RGBA8888);
            const QPixmap pixels = QPixmap::fromImage(frame.copy());
            QPixmap viewport_pixels = pixels.scaled(viewport_->size(), Qt::KeepAspectRatio,
                                                     Qt::SmoothTransformation);
            viewport_pixmap_offset_ = {
                (viewport_->width() - viewport_pixels.width()) / 2,
                (viewport_->height() - viewport_pixels.height()) / 2};
            decorate_viewport(viewport_pixels);
            viewport_->setPixmap(viewport_pixels);
            render_sensor_preview();
            last_scene_signature_ = scene_signature();
            render_dirty_ = false;
            ++rendered_frames_;
            render_time_ms_ += render_timer.nsecsElapsed() / 1000000.0;
        } catch (const std::exception& error) {
            viewport_->setText(QString("Vulkan render error: %1").arg(error.what()));
        }
#endif
    }

    void update_performance_metrics() {
        const qint64 elapsed = performance_clock_.elapsed();
        if (elapsed < 1000) return;
        render_fps_ = static_cast<double>(rendered_frames_) * 1000.0 / elapsed;
        average_render_ms_ = rendered_frames_ == 0
            ? 0.0 : render_time_ms_ / static_cast<double>(rendered_frames_);
        rendered_frames_ = 0;
        render_time_ms_ = 0.0;
        performance_clock_.restart();
    }

    [[nodiscard]] std::uint64_t scene_signature() const {
        std::uint64_t signature = 14695981039346656037ULL;
        const auto mix = [&signature](std::uint64_t value) {
            signature ^= value;
            signature *= 1099511628211ULL;
        };
        for (const auto entity : world_->registry().view<sim::TransformComponent>()) {
            mix(entity);
            const auto& value = world_->registry().get<sim::TransformComponent>(entity).world;
            mix(std::hash<double>{}(value.translation.x));
            mix(std::hash<double>{}(value.translation.y));
            mix(std::hash<double>{}(value.translation.z));
            mix(std::hash<double>{}(value.rotation.w));
            mix(std::hash<double>{}(value.rotation.x));
            mix(std::hash<double>{}(value.rotation.y));
            mix(std::hash<double>{}(value.rotation.z));
            mix(std::hash<double>{}(value.scale.x));
            mix(std::hash<double>{}(value.scale.y));
            mix(std::hash<double>{}(value.scale.z));
        }
        return signature;
    }

    void update_status() {
        statusBar()->showMessage(QString("Simulation time: %1 s | dt: %2 s")
            .arg(world_->time().state().simulation_time).arg(world_->time().state().fixed_time_step) +
            QString(" | render: %1 FPS, %2 ms").arg(render_fps_, 0, 'f', 1)
                                                   .arg(average_render_ms_, 0, 'f', 1));
    }

    void log(const QString& message) {
        SIM_LOG_INFO("Editor", message.toStdString());
        log_->appendPlainText(message);
    }

    sim::Simulator simulator_;
    sim::World* world_;
    sim::ResourceManager resources_;
    HierarchyTree* hierarchy_{nullptr};
    QWidget* inspector_widget_{nullptr};
    QWidget* physics_widget_{nullptr};
    QWidget* ifps_widget_{nullptr};
    QWidget* camera_widget_{nullptr};
    QLineEdit* name_edit_{nullptr};
    std::array<QDoubleSpinBox*, 3> position_edit_{};
    std::array<QDoubleSpinBox*, 3> rotation_edit_{};
    std::array<QDoubleSpinBox*, 3> scale_edit_{};
    QCheckBox* visible_edit_{nullptr};
    QSpinBox* min_lod_edit_{nullptr};
    QSpinBox* max_lod_edit_{nullptr};
    QWidget* light_widget_{nullptr};
    QComboBox* light_type_edit_{nullptr};
    QCheckBox* light_enabled_edit_{nullptr};
    QPushButton* light_color_edit_{nullptr};
    QDoubleSpinBox* light_intensity_edit_{nullptr};
    QDoubleSpinBox* light_range_edit_{nullptr};
    QDoubleSpinBox* light_decay_edit_{nullptr};
    QDoubleSpinBox* light_inner_cone_edit_{nullptr};
    QDoubleSpinBox* light_outer_cone_edit_{nullptr};
    QDoubleSpinBox* light_angular_diameter_edit_{nullptr};
    QColor light_color_{Qt::white};
    QCheckBox* body_enabled_edit_{nullptr};
    QComboBox* body_type_edit_{nullptr};
    QCheckBox* gravity_edit_{nullptr};
    QDoubleSpinBox* mass_edit_{nullptr};
    std::array<QDoubleSpinBox*, 3> friction_edit_{};
    QDoubleSpinBox* restitution_edit_{nullptr};
    QCheckBox* ifps_show_edit_{nullptr};
    QSpinBox* ifps_count_edit_{nullptr};
    QComboBox* ifps_mode_edit_{nullptr};
    std::array<QCheckBox*, 8> ifps_level_edits_{};
    QComboBox* camera_type_edit_{nullptr};
    QLineEdit* camera_frame_edit_{nullptr};
    QDoubleSpinBox* camera_rate_edit_{nullptr};
    QSpinBox* camera_width_edit_{nullptr};
    QSpinBox* camera_height_edit_{nullptr};
    QDoubleSpinBox* camera_fov_edit_{nullptr};
    QDoubleSpinBox* camera_near_edit_{nullptr};
    QDoubleSpinBox* camera_far_edit_{nullptr};
    QComboBox* camera_preview_edit_{nullptr};
    QLabel* parent_label_{nullptr};
    QLabel* components_label_{nullptr};
    bool inspector_updating_{false};
    QPlainTextEdit* log_{nullptr};
    QPlainTextEdit* agent_log_{nullptr};
    QLabel* sensor_view_{nullptr};
    QComboBox* physics_backend_{nullptr};
    QComboBox* collision_backend_{nullptr};
    QComboBox* render_backend_{nullptr};
    QLabel* backend_status_{nullptr};
    sim::BackendConfig backend_configuration_{};
    ViewportLabel* viewport_{nullptr};
    QTimer* frame_timer_{nullptr};
    QElapsedTimer frame_clock_;
    QElapsedTimer performance_clock_;
    double gui_real_time_factor_{1.0};
    double step_accumulator_{0.0};
    std::uint64_t rendered_frames_{0};
    double render_time_ms_{0.0};
    double render_fps_{0.0};
    double average_render_ms_{0.0};
#if defined(SIM_HAS_VULKAN)
    std::unique_ptr<sim::VulkanBackend> renderer_;
    std::unique_ptr<sim::VulkanBackend> sensor_renderer_;
#endif
    std::unique_ptr<sim::SensorSystem> sensor_system_;
    sim::PluginManager plugins_;
    std::shared_ptr<sim::IPhysicsBackend> physics_;
    std::shared_ptr<sim::ICollisionBackend> collision_;
    QTcpSocket* agent_socket_{nullptr};
    QTcpServer* control_server_{nullptr};
    QNetworkAccessManager* network_manager_{nullptr};
    ControlOptions control_options_;
    QString control_descriptor_path_;
    QStringList control_endpoints_;
    std::unordered_map<QTcpSocket*, QByteArray> control_buffers_;
    std::unordered_map<QTcpSocket*, std::string> control_workspaces_;
    std::unordered_map<std::string, sim::EntityId> control_aliases_;
    std::unordered_map<std::string, sim::EntityId> control_trajectories_;
    std::uint64_t control_scene_revision_{0};
    std::uint64_t control_scene_generation_{1};
    std::unordered_map<std::string, std::optional<CollisionWireframe>> collision_wireframes_;
    std::unordered_map<sim::EntityId, sim::IFPSSphereTree> ifps_tree_cache_;
    sim::IFPSSphereTreeCache ifps_build_cache_;
    std::unordered_map<sim::EntityId, std::uint32_t> ifps_level_masks_;
    QTimer* agent_poll_{nullptr};
    QByteArray agent_response_buffer_;
    quint64 agent_request_id_{1};
    std::unordered_map<quint64, sim::EntityId> agent_entities_;
    std::unordered_map<sim::EntityId, QTreeWidgetItem*> entity_items_;
    sim::Vec3 camera_target_{};
    double camera_distance_{5.0};
    double camera_yaw_{0.0};
    double camera_elevation_{0.25};
    TransformTool transform_tool_{TransformTool::Move};
    QPoint gizmo_center_{};
    QPoint viewport_pixmap_offset_{};
    QPainterPath gizmo_hit_path_{};
    bool gizmo_visible_{false};
    bool gizmo_drag_active_{false};
    QString last_hierarchy_error_;
    bool gizmo_transform_changed_{false};
    bool show_colliders_{false};
    bool render_dirty_{true};
    std::uint64_t last_scene_signature_{0};
};

int main(int argc, char** argv) {
    QApplication application(argc, argv);
    bool smoke_test = std::getenv("SIM_EDITOR_SMOKE_TEST") != nullptr;
    ControlOptions control;
    for (int index = 1; index < argc; ++index) {
        smoke_test = smoke_test || std::string_view(argv[index]) == "--smoke-test";
        if (std::string_view(argv[index]) == "--control-port" && index + 1 < argc) {
            bool valid = false;
            control.port = QString::fromLocal8Bit(argv[++index]).toUShort(&valid);
            control.enabled = valid;
        } else if (std::string_view(argv[index]) == "--control-host" && index + 1 < argc) {
            control.host = QString::fromUtf8(argv[++index]);
        } else if (std::string_view(argv[index]) == "--registry-dir" && index + 1 < argc) {
            control.registry_directory = QString::fromUtf8(argv[++index]);
        } else if (std::string_view(argv[index]) == "--instance-name" && index + 1 < argc) {
            control.instance_name = QString::fromUtf8(argv[++index]);
        } else if (std::string_view(argv[index]) == "--instance-id" && index + 1 < argc) {
            control.instance_id = QString::fromUtf8(argv[++index]);
        } else if (std::string_view(argv[index]) == "--advertise" && index + 1 < argc) {
            control.advertise.append(QString::fromUtf8(argv[++index]));
        }
    }
    if (smoke_test) return 0;
    MainWindow window(false, std::move(control));
    window.show();
    return application.exec();
}
