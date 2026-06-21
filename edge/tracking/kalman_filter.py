from __future__ import annotations

"""
Constant-velocity Kalman filter for bounding-box tracking — BoT-SORT variant.

State is 8-D:  [cx, cy, w, h, vcx, vcy, vw, vh]
Measurement is 4-D: [cx, cy, w, h]  (centre x/y, width, height)

This is the BoT-SORT formulation: unlike classic SORT/DeepSORT (which track
aspect-ratio `a` and height `h`), we track width and height *directly*. That
keeps the box shape stable through partial occlusion (a half-occluded person
whose detected box narrows doesn't get its height yanked around by an
aspect-ratio coupling), which is exactly the crossover case we care about.

Pure numpy — no torch, no scipy needed here.
"""

import numpy as np


class KalmanFilterWH:
    """Kalman filter tracking (cx, cy, w, h) with constant velocity."""

    def __init__(self) -> None:
        ndim, dt = 4, 1.0

        # State transition: position += velocity (constant velocity model).
        self._motion_mat = np.eye(2 * ndim, 2 * ndim, dtype=np.float32)
        for i in range(ndim):
            self._motion_mat[i, ndim + i] = dt
        # Measurement matrix: we observe position (cx, cy, w, h) only.
        self._update_mat = np.eye(ndim, 2 * ndim, dtype=np.float32)

        # Noise is scaled by box size so big/near boxes tolerate more motion
        # than small/far ones. These weights are the BoT-SORT defaults.
        self._std_weight_position = 1.0 / 20
        self._std_weight_velocity = 1.0 / 160

    # ---- init --------------------------------------------------------
    def initiate(self, measurement: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Create a track from an unassociated measurement (cx, cy, w, h)."""
        mean_pos = measurement.astype(np.float32)
        mean_vel = np.zeros_like(mean_pos)
        mean = np.r_[mean_pos, mean_vel]

        w, h = measurement[2], measurement[3]
        std = np.array([
            2 * self._std_weight_position * w,
            2 * self._std_weight_position * h,
            2 * self._std_weight_position * w,
            2 * self._std_weight_position * h,
            10 * self._std_weight_velocity * w,
            10 * self._std_weight_velocity * h,
            10 * self._std_weight_velocity * w,
            10 * self._std_weight_velocity * h,
        ], dtype=np.float32)
        covariance = np.diag(np.square(std))
        return mean, covariance

    # ---- predict -----------------------------------------------------
    def predict(self, mean: np.ndarray, covariance: np.ndarray
                ) -> tuple[np.ndarray, np.ndarray]:
        w, h = mean[2], mean[3]
        std_pos = [
            self._std_weight_position * w,
            self._std_weight_position * h,
            self._std_weight_position * w,
            self._std_weight_position * h,
        ]
        std_vel = [
            self._std_weight_velocity * w,
            self._std_weight_velocity * h,
            self._std_weight_velocity * w,
            self._std_weight_velocity * h,
        ]
        motion_cov = np.diag(np.square(np.r_[std_pos, std_vel]).astype(np.float32))

        mean = self._motion_mat @ mean
        covariance = self._motion_mat @ covariance @ self._motion_mat.T + motion_cov
        return mean, covariance

    # ---- project to measurement space --------------------------------
    def project(self, mean: np.ndarray, covariance: np.ndarray
                ) -> tuple[np.ndarray, np.ndarray]:
        w, h = mean[2], mean[3]
        std = np.array([
            self._std_weight_position * w,
            self._std_weight_position * h,
            self._std_weight_position * w,
            self._std_weight_position * h,
        ], dtype=np.float32)
        innovation_cov = np.diag(np.square(std))

        mean = self._update_mat @ mean
        covariance = self._update_mat @ covariance @ self._update_mat.T
        return mean, covariance + innovation_cov

    # ---- correct -----------------------------------------------------
    def update(self, mean: np.ndarray, covariance: np.ndarray,
               measurement: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        projected_mean, projected_cov = self.project(mean, covariance)

        # Kalman gain via solving the linear system (numerically stable).
        kalman_gain = np.linalg.solve(
            projected_cov.T,
            (covariance @ self._update_mat.T).T,
        ).T
        innovation = measurement.astype(np.float32) - projected_mean

        new_mean = mean + innovation @ kalman_gain.T
        new_covariance = covariance - kalman_gain @ projected_cov @ kalman_gain.T
        return new_mean, new_covariance
